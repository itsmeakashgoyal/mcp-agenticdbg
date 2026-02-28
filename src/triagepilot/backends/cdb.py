"""CDB (Console Debugger) backend for Windows crash dump analysis."""

import logging
import os
import re
import subprocess
import threading
import time
from typing import List, Optional

from .base import DebuggerError, DebuggerSession

logger = logging.getLogger(__name__)

SLOW_COMMAND_PREFIXES = (
    ".reload",
    "!analyze",
    ".symfix",
    ".sympath",
)

PROMPT_REGEX = re.compile(r"^\d+:\d+>\s*$")
COMMAND_MARKER = ".echo COMMAND_COMPLETED_MARKER"
COMMAND_MARKER_PATTERN = re.compile(r"COMMAND_COMPLETED_MARKER")

DEFAULT_CDB_PATHS = [
    r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe",
    r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x86\cdb.exe",
    r"C:\Program Files\Debugging Tools for Windows (x64)\cdb.exe",
    r"C:\Program Files\Debugging Tools for Windows (x86)\cdb.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\cdbX64.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\cdbX86.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\cdbARM64.exe"),
]


class CDBError(DebuggerError):
    """Exception for CDB-related errors."""
    pass


class CDBSession(DebuggerSession):
    """Manages a CDB debugging session on Windows."""

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
    def find_debugger_executable(custom_path: Optional[str] = None) -> Optional[str]:
        if custom_path and os.path.isfile(custom_path):
            return custom_path
        for path in DEFAULT_CDB_PATHS:
            if os.path.isfile(path):
                return path
        return None

    @staticmethod
    def get_local_dumps_path() -> Optional[str]:
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

    # -- CDB-specific helpers -----------------------------------------------

    def _normalize_symbols_path(self, symbols_path: Optional[str]) -> Optional[str]:
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

    def _normalize_image_path(self, image_path: Optional[str]) -> Optional[str]:
        """Normalize image path input for CDB ``-i``."""
        if not image_path:
            return image_path
        normalized_parts = []
        for raw_part in image_path.split(";"):
            part = raw_part.strip()
            if not part:
                continue
            lower_part = part.lower()
            if (lower_part.endswith(".exe") or lower_part.endswith(".dll")) and os.path.isfile(part):
                part = os.path.dirname(part)
            normalized_parts.append(part)
        return ";".join(normalized_parts) if normalized_parts else image_path

    def _configure_symbol_options(self):
        """Configure CDB for efficient symbol resolution.

        SYMOPT_NO_PROMPTS (0x80000) prevents blocking on network prompts.
        SYMOPT_FAVOR_COMPRESSED (0x800000) prefers compressed downloads.
        """
        self.send_command(".symopt+ 0x880000")

    def _read_output(self):
        """Background thread to continuously read CDB output."""
        if not self.process or not self.process.stdout:
            return
        buffer: List[str] = []
        try:
            for line in self.process.stdout:
                line = line.rstrip()
                logger.debug("CDB > %s", line)
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
            logger.error("CDB output reader error: %s", e)

    def _wait_for_prompt(self, timeout=None):
        try:
            self.ready_event.clear()
            self.process.stdin.write(f"{COMMAND_MARKER}\n")
            self.process.stdin.flush()
            if not self.ready_event.wait(timeout=timeout or self.timeout):
                raise CDBError("Timed out waiting for CDB prompt")
        except IOError as e:
            raise CDBError(f"Failed to communicate with CDB: {e}")

    def send_command(self, command: str, timeout: Optional[int] = None) -> List[str]:
        if not self.process:
            raise CDBError("CDB process is not running")

        with self.command_lock:
            self.ready_event.clear()
            with self.lock:
                self.output_lines = []

            try:
                self.process.stdin.write(f"{command}\n{COMMAND_MARKER}\n")
                self.process.stdin.flush()
            except IOError as e:
                raise CDBError(f"Failed to send command: {e}")

            if timeout is None and self._is_slow_command(command):
                self._wait_with_activity_timeout(command)
            else:
                fixed_timeout = timeout if timeout is not None else self.timeout
                if not self.ready_event.wait(timeout=fixed_timeout):
                    raise CDBError(f"Command timed out after {fixed_timeout} seconds: {command}")

            with self.lock:
                result = self.output_lines.copy()
                self.output_lines = []
            return result

    def _is_slow_command(self, command: str) -> bool:
        normalized = command.strip().lower()
        return normalized.startswith(SLOW_COMMAND_PREFIXES)

    def _wait_with_activity_timeout(self, command: str):
        """Wait for a slow command using activity-based timeout.

        Keeps waiting as long as CDB produces output; only raises after
        ``idle_limit`` consecutive seconds of silence.
        """
        idle_limit = max(self.timeout, 60)
        with self.lock:
            self._last_output_time = time.monotonic()

        while True:
            if self.ready_event.wait(timeout=5):
                return
            if self.process and self.process.poll() is not None:
                raise CDBError(f"CDB process exited unexpectedly during: {command}")
            with self.lock:
                idle_seconds = time.monotonic() - self._last_output_time
            if idle_seconds >= idle_limit:
                raise CDBError(
                    f"Command appears stuck (no output for {int(idle_seconds)}s): {command}"
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
