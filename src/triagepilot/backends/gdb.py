"""GDB backend for Linux crash dump analysis.

Architecture
------------
- MI (Machine Interface) mode by default: commands are token-tagged,
  responses are routed by token to per-command ``_PendingMI`` objects each
  owning a ``threading.Event``.  No shared buffer that could be corrupted by
  concurrent calls or overwritten between commands.
- CLI fallback mode: marker-based delimiting with activity-based timeout
  for slow commands.
- ``_stop_event`` signals the reader thread to exit cleanly on shutdown.
- Pipes (stdin/stdout/stderr) are closed explicitly in ``shutdown()``.
- ``_token_lock`` makes token allocation atomic independently of the
  heavier ``_command_lock``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .base import DebuggerError, DebuggerSession

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# CLI fallback mode markers
COMMAND_MARKER = 'printf "GDB_COMMAND_COMPLETED_MARKER\\n"'
COMMAND_MARKER_TOKEN = "GDB_COMMAND_COMPLETED_MARKER"

# MI line patterns
_MI_RESULT_RE = re.compile(r"^(\d+)\^(done|running|connected|error|exit)(,.*)?$")
_MI_ASYNC_RE = re.compile(r"^(\d+)?[*+=]")
_GDB_PROMPT_RE = re.compile(r"^\(gdb\)\s*$")

# Commands that produce a lot of output and need activity-based timeout
_SLOW_PREFIXES = (
    "info sharedlibrary",
    "info shared",
    "info threads",
    "thread apply all",
    "bt full",
    "info all-registers",
    "disassemble",
    "x/",
    "maintenance info",
)

DEFAULT_GDB_PATHS = [
    "/usr/bin/gdb",
    "/usr/local/bin/gdb",
    "/bin/gdb",
]

_MI_TOKEN_START = 1000
_ACTIVITY_IDLE_LIMIT_S = 120  # seconds of no output before giving up on slow cmds


# ---------------------------------------------------------------------------
# MI Parser
# ---------------------------------------------------------------------------


class MIParseError(ValueError):
    """Raised when GDB MI output cannot be parsed."""


class MIParser:
    """Recursive-descent parser for GDB Machine Interface output.

    Grammar (from the GDB MI specification, §27.2.2)::

        result-list  : result (',' result)*
        result       : name '=' value
        value        : const | tuple | list
        const        : c-string
        tuple        : '{}' | '{' result (',' result)* '}'
        list         : '[]'
                     | '[' value  (',' value )*  ']'
                     | '[' result (',' result)* ']'
        c-string     : '"' (escape | non-quote)* '"'

    Key design choices
    ------------------
    * Cursor-based: every ``_parse_*`` method takes the raw string and an
      integer position; returns ``(parsed_object, new_position)``.
    * Commas inside quoted strings are handled correctly because the parser
      never splits on bare commas.
    * Duplicate keys (e.g. multiple ``frame=`` inside a result-list used as
      a GDB list item) are folded into a Python list.
    * ``parse`` is the main public entry point; the ``_parse_result_record``
      and ``_parse_stream_record`` helpers are used by the reader thread.
    """

    __slots__ = ("_s", "_i")

    def __init__(self, text: str) -> None:
        self._s = text
        self._i = 0

    # ------------------------------------------------------------------
    # Public class-level entry points
    # ------------------------------------------------------------------

    @classmethod
    def parse(cls, results_str: str) -> dict[str, Any]:
        """Parse a comma-separated result-list string into a dict.

        ``results_str`` is the text that follows the leading comma in a
        ``^done`` result record, e.g.::

            frame={level="0",addr="0x00400a10",func="main"}

        Returns an empty dict on parse failure (logs at DEBUG level).
        """
        if not results_str:
            return {}
        try:
            return cls(results_str.strip())._result_list()
        except Exception as exc:  # noqa: BLE001
            logger.debug("MI parse error (%s) on: %.200s", exc, results_str)
            return {}

    @classmethod
    def parse_result_record(cls, line: str) -> dict[str, Any] | None:
        """Parse a complete MI result-record line.

        Returns a dict with keys ``token``, ``class``, ``results``, or
        ``None`` if the line is not a result record.
        """
        m = _MI_RESULT_RE.match(line)
        if not m:
            return None
        token = int(m.group(1))
        result_class = m.group(2)
        results_str = (m.group(3) or "").lstrip(",")
        return {
            "token": token,
            "class": result_class,
            "results": cls.parse(results_str),
        }

    @classmethod
    def parse_stream_record(cls, line: str) -> tuple[str, str] | None:
        """Parse a MI stream record line (``~``, ``@``, ``&``).

        Returns ``(kind, decoded_text)`` where *kind* is one of
        ``"console"``, ``"target"``, ``"log"``, or ``None`` if the line is
        not a stream record.
        """
        if len(line) < 2:
            return None
        kind_map = {"~": "console", "@": "target", "&": "log"}
        kind = kind_map.get(line[0])
        if kind is None or line[1] != '"':
            return None
        try:
            text, _ = cls(line[1:])._string()
            return kind, text
        except Exception:  # noqa: BLE001
            return kind, line[2:].rstrip('"')

    # ------------------------------------------------------------------
    # Grammar rules
    # ------------------------------------------------------------------

    def _result_list(self) -> dict[str, Any]:
        """result (',' result)*  →  dict"""
        out: dict[str, Any] = {}
        self._ws()
        if not self._ch() or self._ch() in "}]":
            return out
        self._one_result(out)
        while self._ch() == ",":
            self._i += 1
            self._ws()
            if not self._ch() or self._ch() in "}]":
                break
            self._one_result(out)
        return out

    def _one_result(self, out: dict[str, Any]) -> None:
        """Parse ``name=value`` and merge into *out*."""
        name = self._name()
        self._eat("=")
        val = self._value()
        # Fold duplicate keys into a list (GDB sometimes repeats a key)
        if name in out:
            existing = out[name]
            if isinstance(existing, list):
                existing.append(val)
            else:
                out[name] = [existing, val]
        else:
            out[name] = val

    def _name(self) -> str:
        start = self._i
        # MI key chars: letters, digits, hyphens, underscores
        while self._ch() and self._ch() not in '=,{}[]"':
            self._i += 1
        n = self._s[start : self._i].strip()
        if not n:
            raise MIParseError(f"Empty name at pos {self._i}")
        return n

    def _value(self) -> Any:
        self._ws()
        c = self._ch()
        if c == '"':
            v, _ = self._string()
            return v
        if c == "{":
            return self._tuple()
        if c == "[":
            return self._list()
        # Bare unquoted token — uncommon but occurs in some GDB versions
        start = self._i
        while self._ch() and self._ch() not in ",}]":
            self._i += 1
        return self._s[start : self._i].strip()

    def _string(self) -> tuple[str, int]:
        """Parse a C-style quoted string.  Returns (text, end_pos)."""
        self._eat('"')
        parts: list[str] = []
        s = self._s
        i = self._i
        n = len(s)
        while i < n:
            c = s[i]
            if c == '"':
                i += 1
                break
            if c == "\\":
                i += 1
                if i >= n:
                    break
                e = s[i]
                i += 1
                if e == "n":
                    parts.append("\n")
                elif e == "t":
                    parts.append("\t")
                elif e == "r":
                    parts.append("\r")
                elif e == "\\":
                    parts.append("\\")
                elif e == '"':
                    parts.append('"')
                elif e == "0":
                    parts.append("\0")
                elif e in ("x", "X"):
                    h = s[i : i + 2]
                    if len(h) == 2 and all(c in "0123456789abcdefABCDEF" for c in h):
                        parts.append(chr(int(h, 16)))
                        i += 2
                    else:
                        parts.append(e)
                else:
                    parts.append(e)
            else:
                parts.append(c)
                i += 1
        self._i = i
        return "".join(parts), i

    def _tuple(self) -> dict[str, Any]:
        """{} | { result (',' result)* }"""
        self._eat("{")
        self._ws()
        if self._ch() == "}":
            self._i += 1
            return {}
        out = self._result_list()
        self._ws()
        self._eat("}")
        return out

    def _list(self) -> list[Any]:
        """[] | [value,...] | [result,...]"""
        self._eat("[")
        self._ws()
        if self._ch() == "]":
            self._i += 1
            return []

        # Peek: decide whether items are results (name=value) or plain values.
        saved = self._i
        is_result_list = self._looks_like_result()
        self._i = saved

        items: list[Any] = []
        while True:
            self._ws()
            if self._ch() == "]":
                break
            if is_result_list:
                item: dict[str, Any] = {}
                self._one_result(item)
                items.append(item)
            else:
                items.append(self._value())
            self._ws()
            if self._ch() != ",":
                break
            self._i += 1

        self._ws()
        if self._ch() == "]":
            self._i += 1
        return items

    def _looks_like_result(self) -> bool:
        """Peek: is the next non-whitespace token a ``name=`` pair?"""
        self._ws()
        start = self._i
        while self._ch() and self._ch() not in '=,{}[]"':
            self._i += 1
        name = self._s[start : self._i].strip()
        result = bool(name) and self._ch() == "="
        self._i = start
        return result

    # ------------------------------------------------------------------
    # Lexer helpers
    # ------------------------------------------------------------------

    def _ch(self) -> str:
        return self._s[self._i] if self._i < len(self._s) else ""

    def _eat(self, c: str) -> None:
        if self._ch() != c:
            ctx = self._s[max(0, self._i - 10) : self._i + 10]
            raise MIParseError(f"Expected {c!r} at pos {self._i}, got {self._ch()!r}: …{ctx!r}…")
        self._i += 1

    def _ws(self) -> None:
        # Guard against empty string: `"" in "..."` is True in Python (substring
        # containment), which would produce an infinite loop at end-of-input.
        while self._ch() and self._ch() in " \t\r\n":
            self._i += 1


# ---------------------------------------------------------------------------
# Per-command pending state
# ---------------------------------------------------------------------------


@dataclass
class _PendingMI:
    """Holds the in-flight state for a single MI command.

    The reader thread writes to ``console``, ``log``, ``target`` as stream
    records arrive, and signals ``event`` when the result record is seen.
    The sending thread waits on ``event`` then reads the collected state.
    """

    token: int
    event: threading.Event = field(default_factory=threading.Event)
    # Decoded stream lines routed here by the reader thread
    console: list[str] = field(default_factory=list)  # ~"..."
    log: list[str] = field(default_factory=list)  # &"..."
    target: list[str] = field(default_factory=list)  # @"..."
    # Set by reader thread when the token^class,... line arrives
    result_class: str | None = None
    result_str: str = ""  # raw text after '^CLASS,' (without the leading comma)


# ---------------------------------------------------------------------------
# GDB backend errors
# ---------------------------------------------------------------------------


class GDBError(DebuggerError):
    """Exception for GDB-related errors."""


# ---------------------------------------------------------------------------
# GDB Session
# ---------------------------------------------------------------------------


class GDBSession(DebuggerSession):
    """Manages a GDB debugging session on Linux.

    MI mode (default)
    ~~~~~~~~~~~~~~~~~
    Commands are sent as ``TOKEN-mi-command\\n``.  Each command gets its own
    :class:`_PendingMI` registered in ``_pending_map`` before the write so
    the reader thread can route the response even if it arrives before the
    sending thread re-acquires ``_pending_lock``.  Stream records
    (``~"..."``) are routed to ``_active_pending`` which is always the
    current in-flight command (serialised by ``_command_lock``).

    CLI mode (fallback)
    ~~~~~~~~~~~~~~~~~~~
    Commands are followed by a marker ``printf`` command; the reader thread
    signals ``_cli_marker_event`` when the marker appears in output.
    Slow commands use an activity-based timeout that keeps waiting as long
    as output keeps arriving.
    """

    def __init__(
        self,
        dump_path: str,
        debugger_path: str | None = None,
        symbols_path: str | None = None,
        image_path: str | None = None,
        initial_commands: list[str] | None = None,
        timeout: int = 30,
        verbose: bool = False,
        additional_args: list[str] | None = None,
        use_mi: bool = True,
        **_kwargs: Any,
    ) -> None:
        if not dump_path:
            raise ValueError("dump_path must be provided")
        if not os.path.isfile(dump_path):
            raise FileNotFoundError(f"Core dump file not found: {dump_path}")

        self.dump_path = dump_path
        self.symbols_path = symbols_path
        self.image_path = image_path
        self.timeout = timeout
        self.verbose = verbose
        self.use_mi = use_mi

        self.debugger_path = self.find_debugger_executable(debugger_path)
        if not self.debugger_path:
            raise GDBError(
                "Could not find gdb executable. Install GDB or provide a path via --debugger-path."
            )

        # Build argv
        argv: list[str] = [self.debugger_path, "-q", "-nx"]
        if use_mi:
            argv.append("--interpreter=mi2")
        if self.image_path:
            argv.extend([self.image_path, "-c", self.dump_path])
        else:
            argv.extend(["-c", self.dump_path])
        if additional_args:
            argv.extend(additional_args)

        try:
            self.process: subprocess.Popen | None = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise GDBError(f"Failed to start GDB process: {exc}") from exc

        # --- Threading state -----------------------------------------------
        # Serialize all MI / CLI command sends
        self._command_lock = threading.Lock()

        # MI token allocation (separate lightweight lock)
        self._token_lock = threading.Lock()
        self._mi_token: int = _MI_TOKEN_START

        # Per-command response routing: token → _PendingMI
        self._pending_lock = threading.Lock()
        self._pending_map: dict[int, _PendingMI] = {}
        # Pointer to the command currently receiving stream output; always
        # the same object as self._pending_map[current_token] while a
        # command is in-flight.
        self._active_pending: _PendingMI | None = None

        # Initialization barrier
        self._init_event = threading.Event()
        self._initialized = False

        # Reader thread lifecycle
        self._stop_event = threading.Event()
        self._last_output_time = time.monotonic()

        # CLI mode state
        self._cli_lock = threading.Lock()
        self._cli_buffer: list[str] = []
        self._cli_marker_event = threading.Event()
        self._cli_marker_seen_time = 0.0

        # Start reader thread before waiting for init
        self._reader_thread = threading.Thread(
            target=self._read_output,
            name="gdb-reader",
            daemon=True,
        )
        self._reader_thread.start()

        # Wait for GDB to be ready; clean up on failure
        try:
            self._wait_for_init()
        except Exception:
            self._stop_event.set()
            self._force_kill()
            raise

        # Configure session after initialization
        self._configure_session(symbols_path, initial_commands)

    # ------------------------------------------------------------------
    # DebuggerSession abstract method implementations
    # ------------------------------------------------------------------

    def _analysis_command(self) -> str:
        return "bt full"

    def _crash_info_command(self) -> str:
        return "info signal"

    def _stack_trace_command(self) -> str:
        return "bt"

    def _modules_command(self) -> str:
        return "info sharedlibrary"

    def _threads_command(self) -> str:
        return "info threads"

    @staticmethod
    def backend_name() -> str:
        return "GDB"

    @staticmethod
    def find_debugger_executable(custom_path: str | None = None) -> str | None:
        if custom_path and os.path.isfile(custom_path):
            return custom_path
        for p in DEFAULT_GDB_PATHS:
            if os.path.isfile(p):
                return p
        return shutil.which("gdb")

    @staticmethod
    def get_local_dumps_path() -> str | None:
        """Discover where core dumps land on this Linux system.

        Reads ``/proc/sys/kernel/core_pattern`` as the authoritative source.
        Falls back to well-known static paths.
        """
        try:
            with open("/proc/sys/kernel/core_pattern") as f:
                pattern = f.read().strip()
            if pattern.startswith("|"):
                # Piped to a handler (e.g. systemd-coredump)
                systemd_dir = "/var/lib/systemd/coredump"
                if os.path.isdir(systemd_dir):
                    return systemd_dir
            elif pattern.startswith("/"):
                # Absolute path pattern — return the directory portion
                core_dir = os.path.dirname(pattern)
                if core_dir and os.path.isdir(core_dir):
                    return core_dir
            # Relative pattern ("core" or "core.%p") — falls through to statics
        except OSError:
            pass

        for path in [
            "/var/crash",
            "/var/lib/apport/coredump",
            "/var/lib/systemd/coredump",
            "/tmp",
        ]:
            if os.path.isdir(path):
                return path
        return None

    # ------------------------------------------------------------------
    # Public command interface
    # ------------------------------------------------------------------

    def send_command(self, command: str, timeout: int | None = None) -> list[str]:
        """Send a GDB command and return output lines.

        In MI mode the command is wrapped with ``interpreter-exec console``.
        In CLI mode a marker command is appended for delimiting.
        """
        if self.use_mi:
            return self._send_via_interpreter_exec(command, timeout)
        return self._send_cli_command(command, timeout)

    def send_mi_command(self, mi_command: str, timeout: int | None = None) -> dict[str, Any]:
        """Send a raw MI command and return the parsed response dict.

        Only available when ``use_mi=True``.  The returned dict has keys:

        * ``class`` — GDB result class (``done``, ``error``, etc.)
        * ``results`` — parsed MI result payload (dict, may be empty)
        * ``console`` — list of console output lines (``~"..."`` decoded)
        * ``log`` — list of log lines (``&"..."`` decoded)
        """
        if not self.use_mi:
            raise GDBError("MI commands require use_mi=True at session creation")
        return self._send_mi_command(mi_command, timeout)

    # ------------------------------------------------------------------
    # Comprehensive crash analysis (override base)
    # ------------------------------------------------------------------

    def run_crash_analysis(self) -> str:
        """Produce a rich multi-section crash report.

        Combines signal info, full backtrace with locals, register state,
        and all-threads backtraces into a single string suitable for
        downstream source localization and AI-assisted triage.
        """
        sections: list[str] = []

        def _try(label: str, cmd: str, cmd_timeout: int | None = None) -> None:
            try:
                out = self.send_command(cmd, timeout=cmd_timeout or self.timeout)
                if out:
                    sections.append(f"=== {label} ===")
                    sections.extend(out)
                    sections.append("")
            except GDBError as exc:
                logger.debug("run_crash_analysis: %s failed: %s", label, exc)

        _try("Signal / Termination", "info signal", 15)
        _try("Crash Frame", "frame", 10)
        _try("Backtrace (full)", "bt full", self.timeout)
        _try("Registers", "info registers", 15)
        _try("All Threads", "thread apply all bt full", self.timeout)
        _try("Shared Libraries", "info sharedlibrary", self.timeout)

        return "\n".join(sections)

    # ------------------------------------------------------------------
    # Structured crash analysis helpers (for AI triage)
    # ------------------------------------------------------------------

    def get_crash_summary(self) -> dict[str, Any]:
        """Return a structured crash summary using MI commands.

        Keys in the returned dict:

        * ``signal``        — signal name / description (str)
        * ``crash_frame``   — dict with ``func``, ``addr``, ``file``, ``line``
        * ``backtrace``     — list of frame dicts
        * ``threads``       — list of thread dicts from ``-thread-info``
        * ``registers``     — dict mapping register name → hex value
        * ``current_thread_id`` — id of the crashing thread
        """
        summary: dict[str, Any] = {
            "signal": None,
            "crash_frame": {},
            "backtrace": [],
            "threads": [],
            "registers": {},
            "current_thread_id": None,
        }

        if not self.use_mi:
            summary["signal"] = "\n".join(self.send_command("info signal", timeout=15))
            return summary

        try:
            # Thread information (includes current frame)
            r = self._send_mi_command("thread-info")
            if r["class"] == "done":
                summary["threads"] = r["results"].get("threads", [])
                summary["current_thread_id"] = r["results"].get("current-thread-id")

            # Stack frames for the crashing thread
            r = self._send_mi_command("stack-list-frames")
            if r["class"] == "done":
                raw_stack = r["results"].get("stack", [])
                # Items are {"frame": {...}} from result-list parsing
                summary["backtrace"] = [
                    (item.get("frame", item) if isinstance(item, dict) else item)
                    for item in raw_stack
                ]
                if summary["backtrace"]:
                    summary["crash_frame"] = summary["backtrace"][0]

            # Register names + values (zipped)
            names_r = self._send_mi_command("data-list-register-names")
            vals_r = self._send_mi_command("data-list-register-values x")
            if names_r["class"] == "done" and vals_r["class"] == "done":
                names = names_r["results"].get("register-names", [])
                reg_vals = vals_r["results"].get("register-values", [])
                for reg in reg_vals:
                    if isinstance(reg, dict):
                        idx = reg.get("number", "")
                        val = reg.get("value", "")
                        try:
                            name = names[int(idx)] if idx.isdigit() else idx
                        except (IndexError, ValueError):
                            name = idx
                        if name:
                            summary["registers"][name] = val

            # Signal info (no MI equivalent — use console command)
            sig_lines = self.send_command("info signal", timeout=10)
            summary["signal"] = "\n".join(sig_lines)

        except GDBError as exc:
            logger.warning("get_crash_summary partial failure: %s", exc)

        return summary

    def get_thread_backtraces(self, max_frames: int = 100) -> list[dict[str, Any]]:
        """Return per-thread backtraces as a list of structured dicts.

        Each dict has keys: ``id``, ``state``, ``target_id``, ``frames``
        (list of frame dicts) or ``raw`` (string) for CLI mode.
        """
        if not self.use_mi:
            raw = self.send_command("thread apply all bt", timeout=self.timeout)
            return [{"raw": "\n".join(raw)}]

        results: list[dict[str, Any]] = []
        try:
            ti = self._send_mi_command("thread-info")
            threads = ti["results"].get("threads", []) if ti["class"] == "done" else []
        except GDBError:
            threads = []

        for thread in threads:
            tid = thread.get("id", "?")
            try:
                # Select thread before listing its frames
                self._send_mi_command(f"thread-select {tid}")
                sf = self._send_mi_command(f"stack-list-frames 0 {max_frames}")
                frames = sf["results"].get("stack", []) if sf["class"] == "done" else []
                results.append(
                    {
                        "id": tid,
                        "state": thread.get("state"),
                        "target_id": thread.get("target-id"),
                        "frames": [
                            (item.get("frame", item) if isinstance(item, dict) else item)
                            for item in frames
                        ],
                    }
                )
            except GDBError as exc:
                results.append({"id": tid, "error": str(exc)})

        return results

    def get_frame_locals(self, frame_num: int = 0) -> dict[str, Any]:
        """Return local variables and arguments for *frame_num*.

        Uses MI ``-stack-list-locals`` / ``-stack-list-arguments`` for
        structured output; falls back to CLI ``info locals`` / ``info args``
        when MI is disabled.
        """
        out: dict[str, Any] = {"frame": frame_num, "locals": [], "args": []}

        if not self.use_mi:
            out["raw"] = "\n".join(
                self.send_command(f"frame {frame_num}", timeout=10)
                + self.send_command("info locals", timeout=10)
                + self.send_command("info args", timeout=10)
            )
            return out

        try:
            self._send_mi_command(f"stack-select-frame {frame_num}")
            r = self._send_mi_command("stack-list-locals --all-values")
            if r["class"] == "done":
                out["locals"] = r["results"].get("locals", [])

            a = self._send_mi_command(f"stack-list-arguments --all-values {frame_num} {frame_num}")
            if a["class"] == "done":
                frames_arg = a["results"].get("stack-args", [])
                if frames_arg:
                    item = frames_arg[0]
                    if isinstance(item, dict):
                        frame_data = item.get("frame", item)
                        out["args"] = (
                            frame_data.get("args", []) if isinstance(frame_data, dict) else []
                        )
        except GDBError as exc:
            out["error"] = str(exc)

        return out

    def get_variable(self, expr: str) -> str | None:
        """Evaluate *expr* in the current frame.  Returns the value string."""
        if self.use_mi:
            try:
                r = self._send_mi_command(f'data-evaluate-expression "{expr}"')
                if r["class"] == "done":
                    return r["results"].get("value")
            except GDBError:
                pass
        # CLI fallback
        out = self.send_command(f"print {expr}", timeout=10)
        for line in out:
            if line.strip() and not line.startswith("$"):
                return line.strip()
        return None

    def inspect_memory(self, address: str, length: int = 64, unit: str = "b") -> str:
        """Hex-dump *length* units at *address*.

        ``unit`` is a GDB format letter: ``b`` (byte), ``h`` (half-word),
        ``w`` (word), ``g`` (giant/8-byte).
        """
        out = self.send_command(f"x/{length}{unit} {address}", timeout=15)
        return "\n".join(out)

    def get_disassembly(
        self,
        location: str | None = None,
        n_instructions: int = 30,
    ) -> str:
        """Disassemble around the crash point (or *location*).

        Uses MI ``-data-disassemble`` for structured output when available.
        """
        if self.use_mi:
            try:
                if location:
                    mi_cmd = f'data-disassemble -f "{location}" -n {n_instructions} -- 0'
                else:
                    byte_range = n_instructions * 4
                    mi_cmd = f'data-disassemble -s $pc -e "$pc+{byte_range}" -- 0'
                r = self._send_mi_command(mi_cmd, timeout=15)
                if r["class"] == "done":
                    insns = r["results"].get("asm_insns", [])
                    return "\n".join(
                        f"{i.get('address', '?')}  {i.get('inst', '?')}"
                        for i in insns
                        if isinstance(i, dict)
                    )
            except GDBError:
                pass
        loc = location or "$pc"
        out = self.send_command(f"disassemble {loc}", timeout=15)
        return "\n".join(out)

    def get_all_registers(self) -> str:
        """Return all register values (general + floating-point + special)."""
        if self.use_mi:
            try:
                names_r = self._send_mi_command("data-list-register-names")
                vals_r = self._send_mi_command("data-list-register-values x")
                if names_r["class"] == "done" and vals_r["class"] == "done":
                    names = names_r["results"].get("register-names", [])
                    reg_vals = vals_r["results"].get("register-values", [])
                    lines: list[str] = []
                    for reg in reg_vals:
                        if isinstance(reg, dict):
                            idx = reg.get("number", "")
                            val = reg.get("value", "")
                            try:
                                name = names[int(idx)] if idx.isdigit() else idx
                            except (IndexError, ValueError):
                                name = idx
                            if name:
                                lines.append(f"{name:<16} {val}")
                    return "\n".join(lines)
            except GDBError:
                pass
        out = self.send_command("info all-registers", timeout=15)
        return "\n".join(out)

    def get_mapped_memory(self) -> str:
        """Return process memory map (``/proc/pid/maps`` equivalent via GDB)."""
        out = self.send_command("info proc mappings", timeout=15)
        return "\n".join(out)

    def get_inferior_info(self) -> str:
        """Return binary/OS info about the inferior."""
        lines: list[str] = []
        for cmd in ("info inferior", "show version", "info files"):
            try:
                out = self.send_command(cmd, timeout=10)
                if out:
                    lines.extend(out)
            except GDBError:
                pass
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # MI internals
    # ------------------------------------------------------------------

    def _next_token(self) -> int:
        with self._token_lock:
            t = self._mi_token
            self._mi_token += 1
        return t

    def _send_mi_command(self, mi_cmd: str, timeout: int | None = None) -> dict[str, Any]:
        """Send a token-tagged MI command; wait on its dedicated Event.

        Thread safety
        ~~~~~~~~~~~~~
        ``_command_lock`` serialises sends so there is only ever one
        in-flight MI command at a time.  Within that window, ``_pending_map``
        and ``_active_pending`` are updated atomically under ``_pending_lock``
        *before* the bytes are written to stdin so the reader thread can
        always find the pending object immediately.
        """
        if not self.process or not self.process.stdin:
            raise GDBError("GDB process is not running")

        effective_timeout = timeout if timeout is not None else self.timeout
        token = self._next_token()
        pending = _PendingMI(token=token)

        with self._command_lock:
            # Register before writing — reader thread may see the response
            # immediately after flush() returns on a fast machine.
            with self._pending_lock:
                self._pending_map[token] = pending
                self._active_pending = pending

            try:
                cmd_line = f"{token}-{mi_cmd}\n"
                if self.verbose:
                    logger.debug("GDB MI → %s", cmd_line.rstrip())
                self.process.stdin.write(cmd_line)
                self.process.stdin.flush()
            except OSError as exc:
                with self._pending_lock:
                    self._pending_map.pop(token, None)
                    if self._active_pending is pending:
                        self._active_pending = None
                raise GDBError(f"Failed to write MI command: {exc}") from exc

            ok = pending.event.wait(timeout=effective_timeout)

            with self._pending_lock:
                self._pending_map.pop(token, None)
                if self._active_pending is pending:
                    self._active_pending = None

        if not ok:
            raise GDBError(f"MI command timed out after {effective_timeout}s: {mi_cmd!r}")

        return {
            "class": pending.result_class,
            "results": MIParser.parse(pending.result_str),
            "console": pending.console,
            "log": pending.log,
            "target": pending.target,
        }

    def _send_via_interpreter_exec(self, command: str, timeout: int | None = None) -> list[str]:
        """Run a console command through MI ``-interpreter-exec console``."""
        escaped = command.replace("\\", "\\\\").replace('"', '\\"')
        r = self._send_mi_command(f'interpreter-exec console "{escaped}"', timeout)
        if r["class"] == "error":
            msg = r["results"].get("msg", "unknown GDB error")
            raise GDBError(f"GDB error for {command!r}: {msg}")
        return r["console"]

    # ------------------------------------------------------------------
    # CLI mode internals
    # ------------------------------------------------------------------

    def _send_cli_command(self, command: str, timeout: int | None = None) -> list[str]:
        if not self.process or not self.process.stdin:
            raise GDBError("GDB process is not running")

        effective_timeout = timeout if timeout is not None else self.timeout

        with self._command_lock:
            with self._cli_lock:
                self._cli_buffer.clear()
                self._cli_marker_event.clear()
                self._cli_marker_seen_time = 0.0

            try:
                self.process.stdin.write(f"{command}\n{COMMAND_MARKER}\n")
                self.process.stdin.flush()
            except OSError as exc:
                raise GDBError(f"Failed to write command: {exc}") from exc

            if timeout is None and self._is_slow_command(command):
                self._activity_wait(command)
            else:
                if not self._cli_marker_event.wait(timeout=effective_timeout):
                    raise GDBError(f"Command timed out after {effective_timeout}s: {command!r}")

            self._drain_cli()

            with self._cli_lock:
                raw_lines = list(self._cli_buffer)
                self._cli_buffer.clear()

        cleaned: list[str] = []
        for line in raw_lines:
            if COMMAND_MARKER_TOKEN in line:
                before = line.split(COMMAND_MARKER_TOKEN, 1)[0].rstrip()
                if before:
                    cleaned.append(before)
            else:
                cleaned.append(line)
        return cleaned

    @staticmethod
    def _is_slow_command(command: str) -> bool:
        norm = command.strip().lower()
        return norm.startswith(_SLOW_PREFIXES)

    def _activity_wait(self, command: str) -> None:
        """Keep waiting as long as GDB keeps producing output."""
        with self._cli_lock:
            self._last_output_time = time.monotonic()
        while True:
            if self._cli_marker_event.wait(timeout=5):
                return
            if self.process and self.process.poll() is not None:
                raise GDBError(f"GDB process exited during command: {command!r}")
            with self._cli_lock:
                idle = time.monotonic() - self._last_output_time
            if idle >= _ACTIVITY_IDLE_LIMIT_S:
                raise GDBError(f"Command stalled ({idle:.0f}s idle): {command!r}")

    def _drain_cli(
        self,
        *,
        min_grace: float = 0.05,
        idle: float = 0.05,
        max_grace: float = 0.6,
    ) -> None:
        """Wait for trailing output after the CLI marker."""
        start = time.monotonic()
        while True:
            now = time.monotonic()
            with self._cli_lock:
                last = self._last_output_time
                marker_t = self._cli_marker_seen_time
            since = now - marker_t if marker_t else 0.0
            if since >= min_grace and (now - last) >= idle:
                return
            if (now - start) >= max_grace:
                return
            time.sleep(0.02)

    # ------------------------------------------------------------------
    # Output reader thread
    # ------------------------------------------------------------------

    def _read_output(self) -> None:
        """Background thread: reads GDB stdout and dispatches each line."""
        if not self.process or not self.process.stdout:
            return
        try:
            for raw_line in self.process.stdout:
                if self._stop_event.is_set():
                    break
                line = raw_line.rstrip("\r\n")
                if self.verbose:
                    logger.debug("GDB ← %s", line)
                self._last_output_time = time.monotonic()
                if self.use_mi:
                    self._dispatch_mi_line(line)
                else:
                    self._dispatch_cli_line(line)
        except (OSError, ValueError):
            pass
        finally:
            # Wake any thread stuck waiting so it can detect process death
            with self._pending_lock:
                for p in self._pending_map.values():
                    p.event.set()
            self._init_event.set()
            self._cli_marker_event.set()

    def _dispatch_mi_line(self, line: str) -> None:
        # Result record: TOKEN^class[,results]
        m = _MI_RESULT_RE.match(line)
        if m:
            token = int(m.group(1))
            result_class = m.group(2)
            result_str = (m.group(3) or "").lstrip(",")
            with self._pending_lock:
                p = self._pending_map.get(token)
            if p is not None:
                p.result_class = result_class
                p.result_str = result_str
                p.event.set()
            return

        # Initial / inter-command prompt
        if _GDB_PROMPT_RE.match(line):
            if not self._initialized:
                self._initialized = True
                self._init_event.set()
            return

        # Stream records — route to the active pending command
        with self._pending_lock:
            active = self._active_pending
        if active is None:
            return

        decoded = MIParser.parse_stream_record(line)
        if decoded is not None:
            kind, text = decoded
            if kind == "console":
                active.console.append(text)
            elif kind == "log":
                active.log.append(text)
            elif kind == "target":
                active.target.append(text)
        # Async records (*running, *stopped, =thread-*) are intentionally
        # ignored for crash-dump analysis — no live process events occur.

    def _dispatch_cli_line(self, line: str) -> None:
        with self._cli_lock:
            self._last_output_time = time.monotonic()
            self._cli_buffer.append(line)
            if COMMAND_MARKER_TOKEN in line:
                self._cli_marker_seen_time = time.monotonic()
                self._cli_marker_event.set()
        if not self._initialized:
            self._initialized = True
            self._init_event.set()

    # ------------------------------------------------------------------
    # Initialization helpers
    # ------------------------------------------------------------------

    def _wait_for_init(self) -> None:
        if not self._init_event.wait(timeout=self.timeout):
            raise GDBError(
                f"Timed out waiting for GDB to initialize (timeout={self.timeout}s). "
                "Is the dump file valid?"
            )
        if self.process and self.process.poll() is not None:
            raise GDBError("GDB process exited unexpectedly during initialization")

    def _configure_session(
        self,
        symbols_path: str | None,
        initial_commands: list[str] | None,
    ) -> None:
        if self.use_mi:
            for cmd in (
                "gdb-set pagination off",
                "gdb-set confirm off",
                "gdb-set print pretty on",
                "gdb-set print array on",
                "gdb-set print object on",
                "gdb-set print static-members on",
            ):
                try:
                    self._send_mi_command(cmd)
                except GDBError:
                    pass
            if symbols_path:
                for sp in symbols_path.split(":"):
                    sp = sp.strip()
                    if sp:
                        try:
                            self._send_mi_command(f"gdb-set debug-file-directory {sp}")
                        except GDBError:
                            pass
        else:
            for cmd in ("set pagination off", "set confirm off"):
                self.send_command(cmd)
            if symbols_path:
                for sp in symbols_path.split(":"):
                    sp = sp.strip()
                    if sp:
                        self.send_command(f"set debug-file-directory {sp}")

        if initial_commands:
            for cmd in initial_commands:
                if cmd:
                    self.send_command(cmd)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Clean up: signal the reader thread, quit GDB, close all pipes."""
        self._stop_event.set()

        proc = self.process
        if proc is not None:
            if proc.poll() is None:
                # Ask GDB to quit gracefully
                try:
                    if proc.stdin:
                        quit_cmd = f"{self._next_token()}-gdb-exit\n" if self.use_mi else "quit\n"
                        proc.stdin.write(quit_cmd)
                        proc.stdin.flush()
                except OSError:
                    pass

                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()

            # Explicitly close pipes so file descriptors are released now,
            # not whenever the garbage collector runs.
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if pipe:
                        pipe.close()
                except OSError:
                    pass

        self.process = None
        # Give the reader thread time to notice the stop_event and exit
        self._reader_thread.join(timeout=3)

    def _force_kill(self) -> None:
        """Unconditionally kill the process (used on init failure)."""
        proc = self.process
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                pass
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if pipe:
                        pipe.close()
                except OSError:
                    pass
        self.process = None

    def get_session_id(self) -> str:
        return os.path.abspath(self.dump_path)
