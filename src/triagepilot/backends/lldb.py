"""LLDB backend for macOS and Linux crash dump analysis."""

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import List, Optional

from .base import DebuggerError, DebuggerSession

logger = logging.getLogger(__name__)

COMMAND_MARKER = "script print('LLDB_COMMAND_COMPLETED_MARKER')"
COMMAND_MARKER_TOKEN = "LLDB_COMMAND_COMPLETED_MARKER"

DEFAULT_LLDB_PATHS = [
    "/usr/bin/lldb",
    "/usr/local/bin/lldb",
]

if sys.platform == "darwin":
    DEFAULT_LLDB_PATHS.extend([
        "/Applications/Xcode.app/Contents/Developer/usr/bin/lldb",
        "/Library/Developer/CommandLineTools/usr/bin/lldb",
    ])


class LLDBError(DebuggerError):
    """Exception for LLDB-related errors."""
    pass


class LLDBSession(DebuggerSession):
    """Manages an LLDB debugging session on macOS and Linux."""

    def __init__(
        self,
        dump_path: str,
        debugger_path: Optional[str] = None,
        symbols_path: Optional[str] = None,
        image_path: Optional[str] = None,
        initial_commands: Optional[List[str]] = None,
        timeout: int = 10,
        verbose: bool = False,
        additional_args: Optional[List[str]] = None,
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
        self._buffer: List[str] = []
        self._lock = threading.Lock()
        self._command_lock = threading.Lock()
        self._ready_event = threading.Event()
        self._marker_seen = False
        self._marker_seen_time = 0.0
        self._last_output_time = time.monotonic()

        self._reader_thread = threading.Thread(target=self._read_output, name="lldb-output-reader", daemon=True)
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
    def find_debugger_executable(custom_path: Optional[str] = None) -> Optional[str]:
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
    def get_local_dumps_path() -> Optional[str]:
        """Return the default crash dump directory for the current platform."""
        if sys.platform == "darwin":
            diag = os.path.expanduser("~/Library/Logs/DiagnosticReports")
            if os.path.isdir(diag):
                return diag
        else:
            # Linux: core dumps are typically in the current directory or /var/crash
            for path in ["/var/crash", "/var/lib/apport/coredump"]:
                if os.path.isdir(path):
                    return path
        return None

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
        except (IOError, ValueError) as e:
            logger.error("LLDB output reader error: %s", e)

    def _wait_for_ready(self, timeout: Optional[int] = None) -> None:
        # Ensure we can round-trip a simple command.
        self.send_command("version", timeout=timeout or self.timeout)

    def _drain_until_quiet(self, *, min_grace_s: float = 0.05, idle_s: float = 0.05, max_grace_s: float = 0.6) -> None:
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

    def send_command(self, command: str, timeout: Optional[int] = None) -> List[str]:
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
            except IOError as e:
                raise LLDBError(f"Failed to send command: {e}")

            if not self._ready_event.wait(timeout=fixed_timeout):
                raise LLDBError(f"Command timed out after {fixed_timeout} seconds: {command}")

            # Give LLDB a brief chance to finish emitting command output after the marker.
            self._drain_until_quiet()

            with self._lock:
                lines = list(self._buffer)
                self._buffer = []

            # Strip the marker line(s) from output.
            cleaned: List[str] = []
            for line in lines:
                if COMMAND_MARKER_TOKEN in line:
                    before = line.split(COMMAND_MARKER_TOKEN, 1)[0].rstrip()
                    if before:
                        cleaned.append(before)
                    continue
                cleaned.append(line)
            return cleaned

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
        return os.path.abspath(self.dump_path)
