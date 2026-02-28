"""LLDB backend for macOS and Linux crash dump analysis."""

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import List, Optional

from .base import DebuggerError, DebuggerSession

logger = logging.getLogger(__name__)

COMMAND_MARKER = "script print('LLDB_COMMAND_COMPLETED_MARKER')"
COMMAND_MARKER_PATTERN = re.compile(r"LLDB_COMMAND_COMPLETED_MARKER")

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

        self.output_lines: List[str] = []
        self.lock = threading.Lock()
        self.command_lock = threading.Lock()
        self.ready_event = threading.Event()
        self._last_output_time = time.monotonic()
        self.reader_thread = threading.Thread(target=self._read_output)
        self.reader_thread.daemon = True
        self.reader_thread.start()

        try:
            self._wait_for_prompt(timeout=self.timeout)
        except (LLDBError, DebuggerError):
            self.shutdown()
            raise LLDBError("LLDB initialization timed out")

        if symbols_path:
            for path in symbols_path.split(":"):
                path = path.strip()
                if path:
                    self.send_command(f"settings append target.debug-file-search-paths {path}")

        if initial_commands:
            for cmd in initial_commands:
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

    def _read_output(self):
        if not self.process or not self.process.stdout:
            return
        buffer: List[str] = []
        try:
            for line in self.process.stdout:
                line = line.rstrip()
                logger.debug("LLDB > %s", line)
                with self.lock:
                    self._last_output_time = time.monotonic()
                    buffer.append(line)
                    if COMMAND_MARKER_PATTERN.search(line):
                        if buffer and COMMAND_MARKER_PATTERN.search(buffer[-1]):
                            buffer.pop()
                        self.output_lines = buffer
                        buffer = []
                        self.ready_event.set()
        except (IOError, ValueError) as e:
            logger.error("LLDB output reader error: %s", e)

    def _wait_for_prompt(self, timeout=None):
        try:
            self.ready_event.clear()
            self.process.stdin.write(f"{COMMAND_MARKER}\n")
            self.process.stdin.flush()
            if not self.ready_event.wait(timeout=timeout or self.timeout):
                raise LLDBError("Timed out waiting for LLDB prompt")
        except IOError as e:
            raise LLDBError(f"Failed to communicate with LLDB: {e}")

    def send_command(self, command: str, timeout: Optional[int] = None) -> List[str]:
        if not self.process:
            raise LLDBError("LLDB process is not running")

        with self.command_lock:
            self.ready_event.clear()
            with self.lock:
                self.output_lines = []

            try:
                self.process.stdin.write(f"{command}\n{COMMAND_MARKER}\n")
                self.process.stdin.flush()
            except IOError as e:
                raise LLDBError(f"Failed to send command: {e}")

            fixed_timeout = timeout if timeout is not None else self.timeout
            if not self.ready_event.wait(timeout=fixed_timeout):
                raise LLDBError(f"Command timed out after {fixed_timeout} seconds: {command}")

            with self.lock:
                result = self.output_lines.copy()
                self.output_lines = []
            return result

    def shutdown(self):
        try:
            if self.process and self.process.poll() is None:
                try:
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
