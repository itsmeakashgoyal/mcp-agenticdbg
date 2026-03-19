"""LLDB backend for macOS and Linux crash dump analysis."""

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any

from .base import DebuggerError, DebuggerSession

logger = logging.getLogger(__name__)

COMMAND_MARKER = "script print('LLDB_COMMAND_COMPLETED_MARKER')"
COMMAND_MARKER_TOKEN = "LLDB_COMMAND_COMPLETED_MARKER"

DEFAULT_LLDB_PATHS = [
    "/usr/bin/lldb",
    "/usr/local/bin/lldb",
]

if sys.platform == "darwin":
    DEFAULT_LLDB_PATHS.extend(
        [
            "/Applications/Xcode.app/Contents/Developer/usr/bin/lldb",
            "/Library/Developer/CommandLineTools/usr/bin/lldb",
        ]
    )


class LLDBError(DebuggerError):
    """Exception for LLDB-related errors."""

    pass


class LLDBSession(DebuggerSession):
    """Manages an LLDB debugging session on macOS and Linux."""

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
            raise FileNotFoundError(f"Core dump file not found: {dump_path}")

        self.dump_path = dump_path
        self.symbols_path = symbols_path
        self.image_path = image_path
        self.timeout = timeout
        self.verbose = verbose

        self.debugger_path = self.find_debugger_executable(debugger_path)
        if not self.debugger_path:
            raise LLDBError("Could not find lldb. Please install LLDB or provide a valid path.")

        cmd_args = [self.debugger_path, "--no-use-colors"]
        if self.image_path:
            cmd_args.extend([self.image_path, "-c", self.dump_path])
        else:
            cmd_args.extend(["-c", self.dump_path])
        if additional_args:
            cmd_args.extend(additional_args)

        self.process: subprocess.Popen[str] | None = None
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
            raise LLDBError(f"Failed to start LLDB process: {e}")

        # Output collection / delimiting
        self._buffer: list[str] = []
        self._lock = threading.Lock()
        self._command_lock = threading.Lock()
        self._ready_event = threading.Event()
        self._marker_seen = False
        self._marker_seen_time = 0.0
        self._last_output_time = time.monotonic()

        self._reader_thread = threading.Thread(
            target=self._read_output, name="lldb-output-reader", daemon=True
        )
        self._reader_thread.start()

        # Prime the session so we can safely send future commands.
        try:
            self._wait_for_ready(timeout=self.timeout)
        except Exception:
            self.shutdown()
            raise LLDBError("LLDB initialization timed out")

        # Apply session configuration via wrapped commands (ensures proper output ordering).
        if symbols_path:
            for path in symbols_path.split(":"):
                path = path.strip()
                if path:
                    self.send_command(f"settings append target.debug-file-search-paths {path}")

        if initial_commands:
            for cmd in initial_commands:
                if cmd:
                    self.send_command(cmd)

    # -- DebuggerSession abstract methods -----------------------------------

    def _analysis_command(self) -> str:
        return "bt all"

    def _crash_info_command(self) -> str:
        return "thread info"

    def _stack_trace_command(self) -> str:
        return "bt"

    def _modules_command(self) -> str:
        return "image list"

    def _threads_command(self) -> str:
        return "thread list"

    @staticmethod
    def backend_name() -> str:
        return "LLDB"

    @staticmethod
    def find_debugger_executable(custom_path: str | None = None) -> str | None:
        if custom_path and os.path.isfile(custom_path):
            return custom_path
        for path in DEFAULT_LLDB_PATHS:
            if os.path.isfile(path):
                return path
        found = shutil.which("lldb")
        if found:
            return found
        return None

    @staticmethod
    def get_local_dumps_path() -> str | None:
        """Return the default crash dump directory for the current platform."""
        if sys.platform == "darwin":
            # macOS core dumps are written to /cores/core.<pid> by default.
            if os.path.isdir("/cores"):
                return "/cores"
            # Fallback: Apple crash reporter text reports (not binary core dumps,
            # but useful for locating crash context).
            diag = os.path.expanduser("~/Library/Logs/DiagnosticReports")
            if os.path.isdir(diag):
                return diag
        else:
            # Linux: core dumps are typically in the current directory or /var/crash
            for path in ["/var/crash", "/var/lib/apport/coredump"]:
                if os.path.isdir(path):
                    return path
        return None

    # -- DebuggerSession rich-analysis overrides ----------------------------

    def run_crash_analysis(self) -> str:
        """Produce a rich multi-section crash report.

        Combines process status, all-thread backtraces, register state, and
        loaded images into a single string for downstream triage.
        """
        sections: list[str] = []

        def _try(label: str, cmd: str, cmd_timeout: int | None = None) -> None:
            try:
                out = self.send_command(cmd, timeout=cmd_timeout or self.timeout)
                if out:
                    sections.append(f"=== {label} ===")
                    sections.extend(out)
                    sections.append("")
            except LLDBError as exc:
                logger.debug("run_crash_analysis: %s failed: %s", label, exc)

        _try("Process Status", "process status", 10)
        _try("Crash Frame", "frame info", 10)
        _try("Backtrace (full)", "bt", self.timeout)
        _try("All Threads", "bt all", self.timeout)
        _try("Registers", "register read", 15)
        _try("Loaded Images", "image list", self.timeout)

        return "\n".join(sections)

    def get_crash_summary(self) -> dict[str, Any]:
        """Return a structured crash summary.

        Keys in the returned dict:

        * ``signal``        — crash description string
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
            summary["signal"] = "\n".join(self.send_command("process status", timeout=10))
        except LLDBError:
            pass

        try:
            summary["crash_frame"] = self.send_command("frame info", timeout=10)
        except LLDBError:
            pass

        try:
            summary["backtrace"] = self.send_command("bt", timeout=self.timeout)
        except LLDBError:
            pass

        try:
            summary["threads"] = self.send_command("bt all", timeout=self.timeout)
        except LLDBError:
            pass

        try:
            reg_lines = self.send_command("register read", timeout=15)
            for line in reg_lines:
                # Lines look like: "       rax = 0x0000000000000000"
                if "=" in line:
                    parts = line.split("=", 1)
                    name = parts[0].strip()
                    val = parts[1].strip()
                    if name:
                        summary["registers"][name] = val
        except LLDBError:
            pass

        return summary

    def get_thread_backtraces(self, max_frames: int = 100) -> list[dict[str, Any]]:
        """Return per-thread backtraces as a list of structured dicts.

        Each dict has ``id``, ``raw`` (backtrace text for that thread).
        """
        try:
            raw = self.send_command("bt all", timeout=self.timeout)
        except LLDBError as exc:
            return [{"error": str(exc)}]

        # Split the output into per-thread sections.
        # LLDB bt all output starts thread sections with "* thread #N" or "  thread #N"
        threads: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for line in raw:
            if "thread #" in line.lower():
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
            self.send_command(f"frame select {frame_num}", timeout=10)
            locals_out = self.send_command("frame variable", timeout=10)
            args_out = self.send_command("frame variable --show-globals false", timeout=10)
            raw_lines = self.send_command("frame info", timeout=10) + locals_out + args_out
            out["raw"] = "\n".join(raw_lines)
            out["locals"] = locals_out
        except LLDBError as exc:
            out["error"] = str(exc)
        return out

    def get_variable(self, expr: str) -> str | None:
        """Evaluate *expr* in the current frame. Returns the value string."""
        try:
            out = self.send_command(f"expression {expr}", timeout=10)
            for line in out:
                stripped = line.strip()
                if stripped and not stripped.startswith("error:"):
                    return stripped
        except LLDBError:
            pass
        # Fallback: try frame variable for simple names
        try:
            out = self.send_command(f"frame variable {expr}", timeout=10)
            for line in out:
                stripped = line.strip()
                if stripped:
                    return stripped
        except LLDBError:
            pass
        return None

    def inspect_memory(self, address: str, length: int = 64, unit: str = "b") -> str:
        """Hex-dump *length* bytes at *address*.

        ``unit`` is ignored (LLDB uses ``--size`` for element size);
        ``length`` is the byte count.
        """
        out = self.send_command(f"memory read --count {length} --format x {address}", timeout=15)
        return "\n".join(out)

    def get_disassembly(
        self,
        location: str | None = None,
        n_instructions: int = 30,
    ) -> str:
        """Disassemble around the crash point (or *location*)."""
        if location:
            cmd = f"disassemble --name {location} --count {n_instructions}"
        else:
            cmd = f"disassemble --pc --count {n_instructions}"
        try:
            out = self.send_command(cmd, timeout=15)
            return "\n".join(out)
        except LLDBError as exc:
            return f"disassembly failed: {exc}"

    def get_all_registers(self) -> str:
        """Return all register values (general + floating-point + special)."""
        try:
            out = self.send_command("register read --all", timeout=15)
            return "\n".join(out)
        except LLDBError:
            pass
        try:
            out = self.send_command("register read", timeout=15)
            return "\n".join(out)
        except LLDBError as exc:
            return f"register read failed: {exc}"

    def get_mapped_memory(self) -> str:
        """Return process memory map."""
        try:
            out = self.send_command("process status --verbose", timeout=15)
            map_out = self.send_command("target modules list", timeout=15)
            return "\n".join(out + [""] + map_out)
        except LLDBError as exc:
            return f"memory map failed: {exc}"

    def get_inferior_info(self) -> str:
        """Return binary/OS info about the inferior."""
        lines: list[str] = []
        for cmd in ("target list", "image list", "target modules dump symtab"):
            try:
                out = self.send_command(cmd, timeout=10)
                if out:
                    lines.extend(out)
                    lines.append("")
            except LLDBError:
                pass
        return "\n".join(lines)

    # -- LLDB-specific helpers ----------------------------------------------

    def _read_output(self) -> None:
        if not self.process or not self.process.stdout:
            return
        try:
            for raw_line in self.process.stdout:
                line = raw_line.rstrip("\n").rstrip("\r")
                logger.debug("LLDB > %s", line)
                with self._lock:
                    self._last_output_time = time.monotonic()
                    self._buffer.append(line)
                    if COMMAND_MARKER_TOKEN in line:
                        self._marker_seen = True
                        self._marker_seen_time = time.monotonic()
                        self._ready_event.set()
        except (OSError, ValueError) as e:
            logger.error("LLDB output reader error: %s", e)

    def _wait_for_ready(self, timeout: int | None = None) -> None:
        # Ensure we can round-trip a simple command.
        self.send_command("version", timeout=timeout or self.timeout)

    def _drain_until_quiet(
        self, *, min_grace_s: float = 0.05, idle_s: float = 0.05, max_grace_s: float = 0.6
    ) -> None:
        """After marker is observed, wait for LLDB to finish emitting output.

        On some LLDB builds, a command's stdout may arrive slightly after a
        subsequent marker. We mitigate by waiting until output is quiet.
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

    def send_command(self, command: str, timeout: int | None = None) -> list[str]:
        if not self.process or not self.process.stdin:
            raise LLDBError("LLDB process is not running")

        fixed_timeout = timeout if timeout is not None else self.timeout

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
                raise LLDBError(f"Failed to send command: {e}")

            if not self._ready_event.wait(timeout=fixed_timeout):
                raise LLDBError(f"Command timed out after {fixed_timeout} seconds: {command}")

            # Give LLDB a brief chance to finish emitting command output after the marker.
            self._drain_until_quiet()

            with self._lock:
                lines = list(self._buffer)
                self._buffer = []

            # Strip the marker line(s) from output.
            cleaned: list[str] = []
            for line in lines:
                if COMMAND_MARKER_TOKEN in line:
                    before = line.split(COMMAND_MARKER_TOKEN, 1)[0].rstrip()
                    if before:
                        cleaned.append(before)
                    continue
                cleaned.append(line)
            return cleaned

    def send_break(self) -> bool:
        """Send SIGINT to the LLDB process to interrupt execution."""
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            return True
        return False

    def shutdown(self):
        try:
            if self.process and self.process.poll() is None:
                try:
                    if self.process.stdin:
                        self.process.stdin.write("quit\n")
                        self.process.stdin.flush()
                    self.process.wait(timeout=2)
                except Exception:
                    pass
                if self.process.poll() is None:
                    self.process.terminate()
                    self.process.wait(timeout=3)
        except Exception as e:
            logger.warning("Error during LLDB shutdown: %s", e)
        finally:
            self.process = None

    def get_session_id(self) -> str:
        assert self.dump_path is not None
        return os.path.abspath(self.dump_path)
