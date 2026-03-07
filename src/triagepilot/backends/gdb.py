"""GDB backend for Linux crash dump analysis with MI and CLI support."""

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Dict, List, Optional, Any

from .base import DebuggerError, DebuggerSession

logger = logging.getLogger(__name__)

# CLI mode markers
COMMAND_MARKER = 'printf "GDB_COMMAND_COMPLETED_MARKER\\n"'
COMMAND_MARKER_TOKEN = "GDB_COMMAND_COMPLETED_MARKER"
COMMAND_MARKER_PATTERN = re.compile(r"GDB_COMMAND_COMPLETED_MARKER")

# MI mode token pattern
MI_RESULT_PATTERN = re.compile(r'^(\d+)\^(done|running|connected|error|exit)(.*)$')
MI_ASYNC_PATTERN = re.compile(r'^(\d+)?[*+=]')
MI_STREAM_PATTERN = re.compile(r'^[@~&]')

# Slow commands that need activity-based timeout
SLOW_COMMAND_PREFIXES = (
    "info sharedlibrary",
    "info shared",
    "info threads",
    "thread apply all",
)

DEFAULT_GDB_PATHS = [
    "/usr/bin/gdb",
    "/usr/local/bin/gdb",
]


class GDBError(DebuggerError):
    """Exception for GDB-related errors."""
    pass


class GDBSession(DebuggerSession):
    """Manages a GDB debugging session on Linux.
    
    Supports two modes:
    - MI (Machine Interface): for structured, parseable output
    - CLI: for complex commands that need human-readable output
    """

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
        use_mi: bool = True,  # Use MI mode by default for better parsing
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
        self.use_mi = use_mi

        self.debugger_path = self.find_debugger_executable(debugger_path)
        if not self.debugger_path:
            raise GDBError("Could not find gdb. Please install GDB or provide a valid path.")

        # Build command arguments
        cmd_args = [self.debugger_path, "-q", "-nx"]
        
        # Use MI interpreter if requested
        if use_mi:
            cmd_args.extend(["--interpreter=mi2"])
        
        # Load core dump
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
            raise GDBError(f"Failed to start GDB process: {e}")

        # Output collection
        self._buffer: List[str] = []
        self._lock = threading.Lock()
        self._command_lock = threading.Lock()
        self._ready_event = threading.Event()
        self._marker_seen = False
        self._marker_seen_time = 0.0
        self._last_output_time = time.monotonic()
        self._mi_token = 1000  # Start MI tokens from 1000

        self._reader_thread = threading.Thread(
            target=self._read_output, 
            name="gdb-output-reader", 
            daemon=True
        )
        self._reader_thread.start()

        try:
            self._wait_for_ready(timeout=self.timeout)
        except Exception:
            self.shutdown()
            raise GDBError("GDB initialization timed out")

        # Configure session
        if use_mi:
            # MI mode: use -gdb-set commands
            self._send_mi_command("gdb-set pagination off")
            self._send_mi_command("gdb-set confirm off")
            
            if symbols_path:
                for path in symbols_path.split(":"):
                    path = path.strip()
                    if path:
                        self._send_mi_command(f"gdb-set debug-file-directory {path}")
        else:
            # CLI mode: use regular commands
            self.send_command("set pagination off")
            self.send_command("set confirm off")
            
            if symbols_path:
                for path in symbols_path.split(":"):
                    path = path.strip()
                    if path:
                        self.send_command(f"set debug-file-directory {path}")

        if initial_commands:
            for cmd in initial_commands:
                if cmd:
                    self.send_command(cmd)

    # -- DebuggerSession abstract methods -----------------------------------

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
    def find_debugger_executable(custom_path: Optional[str] = None) -> Optional[str]:
        if custom_path and os.path.isfile(custom_path):
            return custom_path
        for path in DEFAULT_GDB_PATHS:
            if os.path.isfile(path):
                return path
        found = shutil.which("gdb")
        if found:
            return found
        return None

    @staticmethod
    def get_local_dumps_path() -> Optional[str]:
        """Return the default crash dump directory on Linux."""
        for path in ["/var/crash", "/var/lib/apport/coredump"]:
            if os.path.isdir(path):
                return path
        return None

    # -- Public command interface -------------------------------------------

    def send_command(self, command: str, timeout: Optional[int] = None) -> List[str]:
        """Send a command and return output lines.
        
        In MI mode, this wraps the command with -interpreter-exec console.
        In CLI mode, uses marker-based delimiting.
        """
        if self.use_mi:
            return self._send_console_command_via_mi(command, timeout)
        else:
            return self._send_cli_command(command, timeout)

    def send_mi_command(self, mi_command: str, timeout: Optional[int] = None) -> Dict[str, Any]:
        """Send an MI command and return parsed result.
        
        Only works when use_mi=True. Returns a dict with:
        - 'class': result class (done, error, etc)
        - 'results': parsed MI result dict
        - 'console': any console output
        """
        if not self.use_mi:
            raise GDBError("MI commands require use_mi=True at session creation")
        return self._send_mi_command(mi_command, timeout)

    # -- Dump triage helpers ------------------------------------------------

    def get_crash_summary(self) -> Dict[str, Any]:
        """Get structured crash information for triage.
        
        Returns a dict with:
        - signal: signal that caused the crash
        - registers: register values
        - backtrace: structured stack frames
        - threads: thread information
        """
        summary = {
            "signal": None,
            "registers": {},
            "backtrace": [],
            "threads": [],
        }
        
        if self.use_mi:
            # Use MI for structured data
            try:
                # Get thread info
                thread_result = self._send_mi_command("thread-info")
                if thread_result.get("class") == "done":
                    summary["threads"] = thread_result.get("results", {}).get("threads", [])
                
                # Get current frame info
                frame_result = self._send_mi_command("stack-info-frame")
                if frame_result.get("class") == "done":
                    frame = frame_result.get("results", {}).get("frame", {})
                    summary["signal"] = frame.get("from", "")
                
                # Get registers
                reg_result = self._send_mi_command("data-list-register-values x")
                if reg_result.get("class") == "done":
                    reg_list = reg_result.get("results", {}).get("register-values", [])
                    for reg in reg_list:
                        if isinstance(reg, dict):
                            name = reg.get("number", "")
                            value = reg.get("value", "")
                            summary["registers"][name] = value
                
                # Get backtrace
                bt_result = self._send_mi_command("stack-list-frames")
                if bt_result.get("class") == "done":
                    summary["backtrace"] = bt_result.get("results", {}).get("stack", [])
            
            except GDBError as e:
                logger.warning("Error getting MI crash summary: %s", e)
        
        return summary

    def get_variable(self, var_name: str) -> Optional[str]:
        """Get the value of a variable."""
        if self.use_mi:
            try:
                result = self._send_mi_command(f"data-evaluate-expression {var_name}")
                if result.get("class") == "done":
                    return result.get("results", {}).get("value", None)
            except GDBError:
                pass
        
        # Fallback to CLI
        output = self.send_command(f"print {var_name}")
        for line in output:
            if line.strip() and not line.startswith("$"):
                return line.strip()
        return None

    # -- Internal MI methods ------------------------------------------------

    def _send_mi_command(self, mi_command: str, timeout: Optional[int] = None) -> Dict[str, Any]:
        """Send an MI command and parse the structured response."""
        if not self.process or not self.process.stdin:
            raise GDBError("GDB process is not running")

        fixed_timeout = timeout if timeout is not None else self.timeout

        with self._command_lock:
            token = self._mi_token
            self._mi_token += 1
            
            self._ready_event.clear()
            with self._lock:
                self._buffer = []
                self._marker_seen = False
                self._expected_token = token

            try:
                cmd_line = f"{token}-{mi_command}\n"
                self.process.stdin.write(cmd_line)
                self.process.stdin.flush()
            except IOError as e:
                raise GDBError(f"Failed to send MI command: {e}")

            if not self._ready_event.wait(timeout=fixed_timeout):
                raise GDBError(f"MI command timed out after {fixed_timeout} seconds: {mi_command}")

            with self._lock:
                lines = list(self._buffer)
                self._buffer = []

            return self._parse_mi_response(lines, token)

    def _parse_mi_response(self, lines: List[str], expected_token: int) -> Dict[str, Any]:
        """Parse MI response lines into structured data."""
        result = {
            "class": None,
            "results": {},
            "console": [],
            "log": [],
        }
        
        for line in lines:
            # Result record: TOKEN^CLASS,RESULTS
            match = MI_RESULT_PATTERN.match(line)
            if match:
                token = int(match.group(1))
                if token == expected_token:
                    result["class"] = match.group(2)
                    results_str = match.group(3)
                    if results_str.startswith(","):
                        result["results"] = self._parse_mi_results(results_str[1:])
                continue
            
            # Console stream: ~"..."
            if line.startswith('~"'):
                console_line = self._unescape_mi_string(line[2:])
                result["console"].append(console_line)
            
            # Log stream: &"..."
            elif line.startswith('&"'):
                log_line = self._unescape_mi_string(line[2:])
                result["log"].append(log_line)
        
        return result

    def _parse_mi_results(self, results_str: str) -> Dict[str, Any]:
        """Parse MI results string into a dict."""
        # Simplified MI parsing - production version would use a proper parser
        results = {}
        try:
            # Very basic parsing - just extract key=value pairs
            pairs = results_str.split(',')
            for pair in pairs:
                if '=' in pair:
                    key, value = pair.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    
                    # Unescape strings
                    if value.startswith('"') and value.endswith('"'):
                        value = self._unescape_mi_string(value)
                    
                    results[key] = value
        except Exception as e:
            logger.debug("Error parsing MI results: %s", e)
        
        return results

    def _unescape_mi_string(self, s: str) -> str:
        """Unescape MI string literals."""
        if s.startswith('"') and s.endswith('"'):
            s = s[1:-1]
        s = s.replace('\\n', '\n')
        s = s.replace('\\t', '\t')
        s = s.replace('\\\\', '\\')
        s = s.replace('\\"', '"')
        return s

    def _send_console_command_via_mi(self, command: str, timeout: Optional[int] = None) -> List[str]:
        """Execute a console command through MI interpreter-exec."""
        # Escape the command for MI
        escaped_cmd = command.replace('\\', '\\\\').replace('"', '\\"')
        mi_cmd = f'interpreter-exec console "{escaped_cmd}"'
        
        result = self._send_mi_command(mi_cmd, timeout)
        
        # Return console output as lines
        return result.get("console", [])

    # -- Internal CLI methods -----------------------------------------------

    def _send_cli_command(self, command: str, timeout: Optional[int] = None) -> List[str]:
        """Send a CLI command with marker-based delimiting."""
        if not self.process or not self.process.stdin:
            raise GDBError("GDB process is not running")

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
                raise GDBError(f"Failed to send command: {e}")

            # Use activity timeout for slow commands
            if timeout is None and self._is_slow_command(command):
                self._wait_with_activity_timeout(command)
            else:
                if not self._ready_event.wait(timeout=fixed_timeout):
                    raise GDBError(f"Command timed out after {fixed_timeout} seconds: {command}")

            self._drain_until_quiet()

            with self._lock:
                lines = list(self._buffer)
                self._buffer = []

            # Strip the marker line(s) from output
            cleaned: List[str] = []
            for line in lines:
                if COMMAND_MARKER_TOKEN in line:
                    before = line.split(COMMAND_MARKER_TOKEN, 1)[0].rstrip()
                    if before:
                        cleaned.append(before)
                    continue
                cleaned.append(line)
            return cleaned

    def _is_slow_command(self, command: str) -> bool:
        """Check if a command is known to be slow."""
        normalized = command.strip().lower()
        return normalized.startswith(SLOW_COMMAND_PREFIXES)

    def _wait_with_activity_timeout(self, command: str):
        """Wait for a slow command using activity-based timeout.
        
        Borrowed from CDB backend: keeps waiting as long as output is produced.
        """
        idle_limit = max(self.timeout, 60)
        with self._lock:
            self._last_output_time = time.monotonic()

        while True:
            if self._ready_event.wait(timeout=5):
                return
            if self.process and self.process.poll() is not None:
                raise GDBError(f"GDB process exited unexpectedly during: {command}")
            with self._lock:
                idle_seconds = time.monotonic() - self._last_output_time
            if idle_seconds >= idle_limit:
                raise GDBError(
                    f"Command appears stuck (no output for {int(idle_seconds)}s): {command}"
                )

    # -- Output reading -----------------------------------------------------

    def _read_output(self) -> None:
        """Background thread to read GDB output."""
        if not self.process or not self.process.stdout:
            return
        try:
            for raw_line in self.process.stdout:
                line = raw_line.rstrip("\n").rstrip("\r")
                logger.debug("GDB > %s", line)
                
                with self._lock:
                    self._last_output_time = time.monotonic()
                    self._buffer.append(line)
                    
                    if self.use_mi:
                        # In MI mode, check for result record with our token
                        match = MI_RESULT_PATTERN.match(line)
                        if match and hasattr(self, '_expected_token'):
                            token = int(match.group(1))
                            if token == self._expected_token:
                                self._marker_seen = True
                                self._marker_seen_time = time.monotonic()
                                self._ready_event.set()
                        
                        # Also check for (gdb) prompt in MI mode
                        if line.strip() == "(gdb)":
                            if not hasattr(self, '_expected_token'):
                                # Initial prompt
                                self._ready_event.set()
                    else:
                        # In CLI mode, check for marker
                        if COMMAND_MARKER_PATTERN.search(line):
                            self._marker_seen = True
                            self._marker_seen_time = time.monotonic()
                            self._ready_event.set()
        
        except (IOError, ValueError) as e:
            logger.error("GDB output reader error: %s", e)

    def _wait_for_ready(self, timeout: Optional[int] = None) -> None:
        """Ensure GDB is ready to accept commands."""
        if self.use_mi:
            # In MI mode, just wait for initial (gdb) prompt
            if not self._ready_event.wait(timeout=timeout or self.timeout):
                raise GDBError("GDB MI initialization timed out")
        else:
            # In CLI mode, send a test command
            self.send_command("show version", timeout=timeout or self.timeout)

    def _drain_until_quiet(
        self, 
        *, 
        min_grace_s: float = 0.05, 
        idle_s: float = 0.05, 
        max_grace_s: float = 0.6
    ) -> None:
        """Wait for output to finish after marker is observed."""
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

    # -- Lifecycle ----------------------------------------------------------

    def shutdown(self):
        """Clean shutdown of GDB process."""
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
            logger.warning("Error during GDB shutdown: %s", e)
        finally:
            self.process = None

    def get_session_id(self) -> str:
        return os.path.abspath(self.dump_path)
