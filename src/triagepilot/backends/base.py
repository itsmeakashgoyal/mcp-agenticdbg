"""Abstract base class for debugger session backends."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class DebuggerError(Exception):
    """Base exception for debugger-related errors."""

    pass


class DebuggerSession(ABC):
    """Abstract base class for platform-specific debugger sessions.

    Each backend (CDB, LLDB, GDB) implements this interface so that the
    tool layer can work with any debugger transparently.
    """

    dump_path: str | None
    symbols_path: str | None
    image_path: str | None
    timeout: int
    verbose: bool

    def __init__(
        self,
        dump_path: str,
        debugger_path: str | None = None,
        symbols_path: str | None = None,
        image_path: str | None = None,
        timeout: int = 30,
        verbose: bool = False,
        **kwargs: object,
    ) -> None:
        self.dump_path = dump_path
        self.symbols_path = symbols_path
        self.image_path = image_path
        self.timeout = timeout
        self.verbose = verbose

    @abstractmethod
    def send_command(self, command: str, timeout: int | None = None) -> list[str]:
        """Send a command to the debugger and return the output lines."""
        ...

    @abstractmethod
    def shutdown(self) -> None:
        """Terminate the debugger process and release resources."""
        ...

    @abstractmethod
    def get_session_id(self) -> str:
        """Return a unique identifier for this session."""
        ...

    def run_crash_analysis(self) -> str:
        """Run the primary crash analysis command and return output.

        CDB: ``!analyze -v``
        LLDB: ``bt all`` + thread info
        GDB: ``bt full`` + thread info
        """
        return "\n".join(self.send_command(self._analysis_command()))

    def get_crash_info(self) -> str:
        """Return basic crash event information."""
        return "\n".join(self.send_command(self._crash_info_command()))

    def get_stack_trace(self) -> str:
        """Return the stack trace of the crashing thread."""
        return "\n".join(self.send_command(self._stack_trace_command()))

    def get_loaded_modules(self) -> str:
        """Return the list of loaded modules/shared libraries."""
        return "\n".join(self.send_command(self._modules_command()))

    def get_threads(self) -> str:
        """Return thread listing."""
        return "\n".join(self.send_command(self._threads_command()))

    @abstractmethod
    def _analysis_command(self) -> str:
        """Return the backend-specific full analysis command."""
        ...

    @abstractmethod
    def _crash_info_command(self) -> str:
        """Return the backend-specific crash info command."""
        ...

    @abstractmethod
    def _stack_trace_command(self) -> str:
        """Return the backend-specific stack trace command."""
        ...

    @abstractmethod
    def _modules_command(self) -> str:
        """Return the backend-specific loaded modules command."""
        ...

    @abstractmethod
    def _threads_command(self) -> str:
        """Return the backend-specific thread listing command."""
        ...

    @staticmethod
    @abstractmethod
    def get_local_dumps_path() -> str | None:
        """Return the platform-specific default crash dump directory."""
        ...

    @staticmethod
    @abstractmethod
    def find_debugger_executable(custom_path: str | None = None) -> str | None:
        """Locate the debugger executable on disk."""
        ...

    @staticmethod
    @abstractmethod
    def backend_name() -> str:
        """Return a human-readable name for this backend (e.g. 'CDB', 'LLDB', 'GDB')."""
        ...

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()
