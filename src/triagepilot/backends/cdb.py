"""CDB (Console Debugger) backend for Windows crash dump analysis."""

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any

from .base import DebuggerError, DebuggerSession

logger = logging.getLogger(__name__)

SLOW_COMMAND_PREFIXES = (
    ".reload",
    "!analyze",
    ".symfix",
    ".sympath",
    "~*kb",
    "~*k",
    "vertarget",
    "!peb",
    "!address",
)

PROMPT_REGEX = re.compile(r"^\d+:\d+>\s*$")
COMMAND_MARKER_TOKEN = "CDB_COMMAND_COMPLETED_MARKER"
COMMAND_MARKER = f".echo {COMMAND_MARKER_TOKEN}"

DEFAULT_CDB_PATHS = [
    r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe",
    r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x86\cdb.exe",
    r"C:\Program Files\Debugging Tools for Windows (x64)\cdb.exe",
    r"C:\Program Files\Debugging Tools for Windows (x86)\cdb.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\cdbX64.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\cdbX86.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\cdbARM64.exe"),
]

# Maximum wall-clock time for any single command (10 minutes).
_MAX_WALL_CLOCK_S = 600


class CDBError(DebuggerError):
    """Exception for CDB-related errors."""

    pass


class CDBSession(DebuggerSession):
    """Manages a CDB debugging session on Windows."""

    def __init__(
        self,
        dump_path: str,
        debugger_path: str | None = None,
        symbols_path: str | None = None,
        image_path: str | None = None,
        initial_commands: list[str] | None = None,
        timeout: int = 10,
        verbose: bool = False,
        additional_args: list[str] | None = None,
        **_kwargs,
    ):
        if not dump_path:
            raise ValueError("dump_path must be provided")

        if not os.path.isfile(dump_path):
            raise FileNotFoundError(f"Dump file not found: {dump_path}")

        self.dump_path = dump_path
        self.symbols_path = symbols_path
        self.image_path = image_path
        self.timeout = timeout
        self.verbose = verbose

        self.debugger_path = self.find_debugger_executable(debugger_path)
        if not self.debugger_path:
            raise CDBError("Could not find cdb.exe. Please provide a valid path.")

        cmd_args = [self.debugger_path, "-z", self.dump_path]

        symbols_path = self._normalize_symbols_path(symbols_path)
        image_path = self._normalize_image_path(image_path)
        self.symbols_path = symbols_path
        self.image_path = image_path

        if symbols_path:
            env_sym_path = os.environ.get("_NT_SYMBOL_PATH", "")
            if env_sym_path:
                combined_path = f"{symbols_path};{env_sym_path}"
            else:
                combined_path = symbols_path
            cmd_args.extend(["-y", combined_path])

        if image_path:
            cmd_args.extend(["-i", image_path])

        if additional_args:
            cmd_args.extend(additional_args)

        try:
            self.process = subprocess.Popen(
                cmd_args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            raise CDBError(f"Failed to start CDB process: {e}")

        # Output collection / delimiting
        self._buffer: list[str] = []
        self._lock = threading.Lock()
        self._command_lock = threading.Lock()
        self._ready_event = threading.Event()
        self._marker_seen = False
        self._marker_seen_time = 0.0
        self._last_output_time = time.monotonic()

        self._reader_thread = threading.Thread(
            target=self._read_output,
            name="cdb-output-reader",
            daemon=True,
        )
        self._reader_thread.start()

        # Backward-compat aliases used by _wait_for_prompt / shutdown
        self.lock = self._lock
        self.command_lock = self._command_lock
        self.ready_event = self._ready_event

        try:
            self._wait_for_prompt(timeout=self.timeout)
        except (CDBError, DebuggerError):
            self.shutdown()
            raise CDBError("CDB initialization timed out")

        self._configure_symbol_options()

        if initial_commands:
            for cmd in initial_commands:
                self.send_command(cmd)

    # -- DebuggerSession abstract methods -----------------------------------

    def _analysis_command(self) -> str:
        return "!analyze -v"

    def _crash_info_command(self) -> str:
        return ".lastevent"

    def _stack_trace_command(self) -> str:
        return "kb"

    def _modules_command(self) -> str:
        return "lm"

    def _threads_command(self) -> str:
        return "~"

    @staticmethod
    def backend_name() -> str:
        return "CDB"

    @staticmethod
    def find_debugger_executable(custom_path: str | None = None) -> str | None:
        if custom_path and os.path.isfile(custom_path):
            return custom_path
        for path in DEFAULT_CDB_PATHS:
            if os.path.isfile(path):
                return path
        found = shutil.which("cdb")
        if found:
            return found
        return None

    @staticmethod
    def get_local_dumps_path() -> str | None:
        """Get the default crash dumps path from Windows registry."""
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps",
            ) as key:
                dump_folder, _ = winreg.QueryValueEx(key, "DumpFolder")
                if os.path.isdir(dump_folder):
                    return dump_folder
        except Exception:
            pass

        default_path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "CrashDumps")
        if os.path.isdir(default_path):
            return default_path
        return None

    # -- DebuggerSession rich-analysis overrides ----------------------------

    def run_crash_analysis(self) -> str:
        """Produce a rich multi-section crash report.

        Combines last-event, full analysis, backtraces, registers, modules,
        and target info into a single string for downstream triage.
        """
        sections: list[str] = []

        def _try(label: str, cmd: str, cmd_timeout: int | None = None) -> None:
            try:
                out = self.send_command(cmd, timeout=cmd_timeout or self.timeout)
                if out:
                    sections.append(f"=== {label} ===")
                    sections.extend(out)
                    sections.append("")
            except CDBError as exc:
                logger.debug("run_crash_analysis: %s failed: %s", label, exc)

        _try("Last Event", ".lastevent", 10)
        _try("Crash Analysis", "!analyze -v")  # slow, activity-based
        _try("Backtrace (current thread)", "kb", 15)
        _try("All Thread Backtraces", "~*kb")  # slow
        _try("Registers", "r", 15)
        _try("Loaded Modules", "lm")
        _try("Target Info", "vertarget", 10)

        return "\n".join(sections)

    def get_crash_summary(self) -> dict[str, Any]:
        """Return a structured crash summary.

        Keys in the returned dict:

        * ``signal``        — crash description string (from ``.lastevent``)
        * ``crash_frame``   — raw frame info lines
        * ``backtrace``     — list of backtrace lines (crashing thread)
        * ``threads``       — raw all-thread backtraces
        * ``registers``     — dict mapping register name → value
        """
        summary: dict[str, Any] = {
            "signal": None,
            "crash_frame": [],
            "backtrace": [],
            "threads": [],
            "registers": {},
        }

        try:
            summary["signal"] = "\n".join(self.send_command(".lastevent", timeout=10))
        except CDBError:
            pass

        try:
            summary["crash_frame"] = self.send_command(".ecxr", timeout=10)
        except CDBError:
            pass

        try:
            summary["backtrace"] = self.send_command("kb", timeout=self.timeout)
        except CDBError:
            pass

        try:
            summary["threads"] = self.send_command("~*kb", timeout=self.timeout)
        except CDBError:
            pass

        try:
            reg_lines = self.send_command("r", timeout=15)
            for line in reg_lines:
                # CDB register output: "rax=0000000000000000 rbx=..."
                for token in line.split():
                    if "=" in token:
                        name, _, val = token.partition("=")
                        name = name.strip()
                        val = val.strip()
                        if name:
                            summary["registers"][name] = val
        except CDBError:
            pass

        return summary

    def get_thread_backtraces(self, max_frames: int = 100) -> list[dict[str, Any]]:
        """Return per-thread backtraces as a list of structured dicts.

        Each dict has ``id``, ``raw`` (backtrace text for that thread).
        """
        try:
            raw = self.send_command("~*kb", timeout=self.timeout)
        except CDBError as exc:
            return [{"error": str(exc)}]

        # Split output into per-thread sections.
        # CDB ~*kb thread headers look like:
        #   "   0  Id: 1234.5678 Suspend: 1 Teb: ..."
        #   ".  1  Id: ..."
        #   "#  2  Id: ..."
        _THREAD_HDR = re.compile(r"^[\s.#*]+\d+\s+Id:")
        threads: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for line in raw:
            if _THREAD_HDR.match(line):
                if current is not None:
                    threads.append(current)
                current = {"id": line.strip(), "raw": [line]}
            elif current is not None:
                current["raw"].append(line)
        if current is not None:
            threads.append(current)

        if not threads:
            threads = [{"id": "all", "raw": raw}]

        for t in threads:
            t["raw"] = "\n".join(t["raw"])

        return threads

    def get_frame_locals(self, frame_num: int = 0) -> dict[str, Any]:
        """Return local variables and arguments for *frame_num*."""
        out: dict[str, Any] = {"frame": frame_num, "locals": [], "args": [], "raw": ""}
        try:
            self.send_command(f".frame {frame_num}", timeout=10)
            locals_out = self.send_command("dv /t", timeout=10)
            out["raw"] = "\n".join(locals_out)
            out["locals"] = locals_out
        except CDBError as exc:
            out["error"] = str(exc)
        return out

    def get_variable(self, expr: str) -> str | None:
        """Evaluate *expr* in the current frame.

        Tries ``??`` (C++ typed evaluation) first, then falls back to
        ``?`` (MASM expression evaluation).
        """
        # Try C++ typed evaluation
        try:
            out = self.send_command(f"?? {expr}", timeout=10)
            for line in out:
                stripped = line.strip()
                if stripped and not stripped.startswith("Couldn't resolve"):
                    return stripped
        except CDBError:
            pass
        # Fallback: MASM expression evaluation
        try:
            out = self.send_command(f"? {expr}", timeout=10)
            for line in out:
                stripped = line.strip()
                if stripped:
                    return stripped
        except CDBError:
            pass
        return None

    def inspect_memory(self, address: str, length: int = 64, unit: str = "b") -> str:
        """Hex-dump *length* bytes at *address*."""
        out = self.send_command(f"db {address} L{length}", timeout=15)
        return "\n".join(out)

    def get_disassembly(
        self,
        location: str | None = None,
        n_instructions: int = 30,
    ) -> str:
        """Disassemble around the crash point (or *location*)."""
        if location:
            cmd = f"u {location} L{n_instructions}"
        else:
            cmd = f"u @rip L{n_instructions}"
        try:
            out = self.send_command(cmd, timeout=15)
            return "\n".join(out)
        except CDBError:
            pass
        # Fallback: try unassemble function at current IP
        try:
            out = self.send_command("uf @rip", timeout=15)
            return "\n".join(out)
        except CDBError as exc:
            return f"disassembly failed: {exc}"

    def get_all_registers(self) -> str:
        """Return all register values."""
        try:
            out = self.send_command("r", timeout=15)
            return "\n".join(out)
        except CDBError as exc:
            return f"register read failed: {exc}"

    def get_mapped_memory(self) -> str:
        """Return process virtual address space map."""
        try:
            out = self.send_command("!address", timeout=self.timeout)
            return "\n".join(out)
        except CDBError as exc:
            return f"memory map failed: {exc}"

    def get_inferior_info(self) -> str:
        """Return binary/OS info about the target."""
        lines: list[str] = []
        for label, cmd in (("Target Info", "vertarget"), ("Process Environment", "!peb")):
            try:
                out = self.send_command(cmd, timeout=15)
                if out:
                    lines.append(f"=== {label} ===")
                    lines.extend(out)
                    lines.append("")
            except CDBError:
                pass
        return "\n".join(lines)

    # -- CDB-specific helpers -----------------------------------------------

    def _normalize_symbols_path(self, symbols_path: str | None) -> str | None:
        """Normalize symbols path input for CDB ``-y``.

        If a segment is a concrete ``.pdb`` path, convert it to the parent
        directory because ``-y`` expects search directories.
        """
        if not symbols_path:
            return symbols_path
        normalized_parts = []
        for raw_part in symbols_path.split(";"):
            part = raw_part.strip()
            if not part:
                continue
            if part.lower().endswith(".pdb") and os.path.isfile(part):
                part = os.path.dirname(part)
            normalized_parts.append(part)
        return ";".join(normalized_parts) if normalized_parts else symbols_path

    def _normalize_image_path(self, image_path: str | None) -> str | None:
        """Normalize image path input for CDB ``-i``."""
        if not image_path:
            return image_path
        normalized_parts = []
        for raw_part in image_path.split(";"):
            part = raw_part.strip()
            if not part:
                continue
            lower_part = part.lower()
            if (lower_part.endswith(".exe") or lower_part.endswith(".dll")) and os.path.isfile(
                part
            ):
                part = os.path.dirname(part)
            normalized_parts.append(part)
        return ";".join(normalized_parts) if normalized_parts else image_path

    def _configure_symbol_options(self):
        """Configure CDB for efficient symbol resolution.

        SYMOPT_NO_PROMPTS (0x80000) prevents blocking on network prompts.
        SYMOPT_FAVOR_COMPRESSED (0x800000) prefers compressed downloads.
        """
        self.send_command(".symopt+ 0x880000")

    def _read_output(self) -> None:
        """Background thread to continuously read CDB output."""
        if not self.process or not self.process.stdout:
            return
        try:
            for raw_line in self.process.stdout:
                line = raw_line.rstrip("\n").rstrip("\r")
                logger.debug("CDB > %s", line)
                with self._lock:
                    self._last_output_time = time.monotonic()
                    if COMMAND_MARKER_TOKEN in line:
                        # Don't append the marker line itself.
                        self._marker_seen = True
                        self._marker_seen_time = time.monotonic()
                        self._ready_event.set()
                    else:
                        self._buffer.append(line)
        except (OSError, ValueError) as e:
            logger.error("CDB output reader error: %s", e)

    def _drain_until_quiet(
        self,
        *,
        min_grace_s: float = 0.05,
        idle_s: float = 0.05,
        max_grace_s: float = 0.6,
    ) -> None:
        """After marker is observed, wait for CDB to finish emitting output.

        On some builds, a command's stdout may arrive slightly after the
        subsequent marker echo.  We mitigate by waiting until output is quiet.
        """
        start = time.monotonic()
        while True:
            now = time.monotonic()
            with self._lock:
                last_out = self._last_output_time
                marker_time = self._marker_seen_time
            since_marker = now - marker_time if marker_time else 0.0
            if since_marker >= min_grace_s and (now - last_out) >= idle_s:
                return
            if (now - start) >= max_grace_s:
                return
            time.sleep(0.02)

    def _wait_for_prompt(self, timeout=None):
        try:
            self._ready_event.clear()
            with self._lock:
                self._buffer = []
                self._marker_seen = False
                self._marker_seen_time = 0.0
            self.process.stdin.write(f"{COMMAND_MARKER}\n")
            self.process.stdin.flush()
            if not self._ready_event.wait(timeout=timeout or self.timeout):
                raise CDBError("Timed out waiting for CDB prompt")
        except OSError as e:
            raise CDBError(f"Failed to communicate with CDB: {e}")

    def send_command(self, command: str, timeout: int | None = None) -> list[str]:
        if not self.process:
            raise CDBError("CDB process is not running")

        with self._command_lock:
            self._ready_event.clear()
            with self._lock:
                self._buffer = []
                self._marker_seen = False
                self._marker_seen_time = 0.0

            try:
                self.process.stdin.write(f"{command}\n{COMMAND_MARKER}\n")
                self.process.stdin.flush()
            except OSError as e:
                raise CDBError(f"Failed to send command: {e}")

            if timeout is None and self._is_slow_command(command):
                self._wait_with_activity_timeout(command)
            else:
                fixed_timeout = timeout if timeout is not None else self.timeout
                if not self._ready_event.wait(timeout=fixed_timeout):
                    raise CDBError(f"Command timed out after {fixed_timeout} seconds: {command}")

            # Give CDB a brief chance to finish emitting output after the marker.
            self._drain_until_quiet()

            with self._lock:
                result = list(self._buffer)
                self._buffer = []
            return result

    def _is_slow_command(self, command: str) -> bool:
        normalized = command.strip().lower()
        return normalized.startswith(SLOW_COMMAND_PREFIXES)

    def _wait_with_activity_timeout(self, command: str):
        """Wait for a slow command using activity-based timeout.

        Keeps waiting as long as CDB produces output; only raises after
        ``idle_limit`` consecutive seconds of silence.  A hard wall-clock
        cap prevents indefinite hangs from commands that trickle output.
        """
        idle_limit = max(self.timeout, 60)
        start_time = time.monotonic()
        with self._lock:
            self._last_output_time = time.monotonic()

        while True:
            if self._ready_event.wait(timeout=5):
                return
            if self.process and self.process.poll() is not None:
                raise CDBError(f"CDB process exited unexpectedly during: {command}")
            with self._lock:
                idle_seconds = time.monotonic() - self._last_output_time
            if idle_seconds >= idle_limit:
                raise CDBError(
                    f"Command appears stuck (no output for {int(idle_seconds)}s): {command}"
                )
            if time.monotonic() - start_time >= _MAX_WALL_CLOCK_S:
                raise CDBError(
                    f"Command exceeded max wall-clock time ({_MAX_WALL_CLOCK_S}s): {command}"
                )

    def shutdown(self):
        try:
            if self.process and self.process.poll() is None:
                try:
                    self.process.stdin.write("q\n")
                    self.process.stdin.flush()
                    self.process.wait(timeout=1)
                except Exception:
                    pass
                if self.process.poll() is None:
                    self.process.terminate()
                    self.process.wait(timeout=3)
        except Exception as e:
            logger.warning("Error during shutdown: %s", e)
        finally:
            self.process = None

    def get_session_id(self) -> str:
        return os.path.abspath(self.dump_path)
