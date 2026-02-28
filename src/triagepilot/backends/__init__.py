"""Debugger backend package -- factory and re-exports."""

from __future__ import annotations

import logging
import shutil
import sys
from typing import Optional

from .base import DebuggerError, DebuggerSession

logger = logging.getLogger(__name__)

__all__ = [
    "DebuggerError",
    "DebuggerSession",
    "create_session",
    "detect_debugger_type",
    "get_local_dumps_path",
]


def detect_debugger_type() -> str:
    """Auto-detect the best debugger backend for the current platform.

    Returns one of ``"cdb"``, ``"lldb"``, or ``"gdb"``.
    """
    if sys.platform == "win32":
        return "cdb"
    if sys.platform == "darwin":
        return "lldb"
    # Linux: prefer GDB, fall back to LLDB
    if shutil.which("gdb"):
        return "gdb"
    if shutil.which("lldb"):
        return "lldb"
    return "gdb"


def _get_backend_class(debugger_type: str) -> type[DebuggerSession]:
    """Import and return the session class for the given debugger type."""
    if debugger_type == "cdb":
        from .cdb import CDBSession
        return CDBSession
    elif debugger_type == "lldb":
        from .lldb import LLDBSession
        return LLDBSession
    elif debugger_type == "gdb":
        from .gdb import GDBSession
        return GDBSession
    else:
        raise ValueError(f"Unknown debugger type: {debugger_type!r}. Use 'auto', 'cdb', 'lldb', or 'gdb'.")


def create_session(
    dump_path: str,
    debugger_path: Optional[str] = None,
    symbols_path: Optional[str] = None,
    image_path: Optional[str] = None,
    timeout: int = 10,
    verbose: bool = False,
    debugger_type: str = "auto",
) -> DebuggerSession:
    """Create a new debugger session using the appropriate backend.

    When *debugger_type* is ``"auto"`` the backend is chosen based on the
    current platform.
    """
    if debugger_type == "auto":
        debugger_type = detect_debugger_type()

    cls = _get_backend_class(debugger_type)
    logger.info("Creating %s session (type=%s)", cls.backend_name(), debugger_type)

    return cls(
        dump_path=dump_path,
        debugger_path=debugger_path,
        symbols_path=symbols_path,
        image_path=image_path,
        timeout=timeout,
        verbose=verbose,
    )


def get_local_dumps_path(debugger_type: str = "auto") -> Optional[str]:
    """Return the default crash dump directory for the given backend."""
    if debugger_type == "auto":
        debugger_type = detect_debugger_type()
    cls = _get_backend_class(debugger_type)
    return cls.get_local_dumps_path()
