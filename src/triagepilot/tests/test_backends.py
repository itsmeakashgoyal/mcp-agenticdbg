"""Tests for the backends package (factory, detection, base class)."""

import sys
import pytest
from unittest.mock import patch

from triagepilot.backends import (
    DebuggerError,
    DebuggerSession,
    detect_debugger_type,
    _get_backend_class,
)
from triagepilot.backends.base import DebuggerSession as BaseSession
from triagepilot.backends.cdb import CDBSession, CDBError
from triagepilot.backends.lldb import LLDBSession, LLDBError
from triagepilot.backends.gdb import GDBSession, GDBError


# ---------------------------------------------------------------------------
# detect_debugger_type
# ---------------------------------------------------------------------------

class TestDetectDebuggerType:
    def test_windows(self):
        with patch.object(sys, "platform", "win32"):
            assert detect_debugger_type() == "cdb"

    def test_darwin(self):
        with patch.object(sys, "platform", "darwin"):
            assert detect_debugger_type() == "lldb"

    def test_linux_gdb_available(self):
        with patch.object(sys, "platform", "linux"), \
             patch("shutil.which", side_effect=lambda x: "/usr/bin/gdb" if x == "gdb" else None):
            assert detect_debugger_type() == "gdb"

    def test_linux_lldb_fallback(self):
        with patch.object(sys, "platform", "linux"), \
             patch("shutil.which", side_effect=lambda x: "/usr/bin/lldb" if x == "lldb" else None):
            assert detect_debugger_type() == "lldb"

    def test_linux_neither(self):
        with patch.object(sys, "platform", "linux"), \
             patch("shutil.which", return_value=None):
            assert detect_debugger_type() == "gdb"


# ---------------------------------------------------------------------------
# _get_backend_class
# ---------------------------------------------------------------------------

class TestGetBackendClass:
    def test_cdb(self):
        assert _get_backend_class("cdb") is CDBSession

    def test_lldb(self):
        assert _get_backend_class("lldb") is LLDBSession

    def test_gdb(self):
        assert _get_backend_class("gdb") is GDBSession

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown debugger type"):
            _get_backend_class("unknown")


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------

class TestErrorHierarchy:
    def test_cdb_error_is_debugger_error(self):
        assert issubclass(CDBError, DebuggerError)

    def test_lldb_error_is_debugger_error(self):
        assert issubclass(LLDBError, DebuggerError)

    def test_gdb_error_is_debugger_error(self):
        assert issubclass(GDBError, DebuggerError)


# ---------------------------------------------------------------------------
# Backend names
# ---------------------------------------------------------------------------

class TestBackendNames:
    def test_cdb_name(self):
        assert CDBSession.backend_name() == "CDB"

    def test_lldb_name(self):
        assert LLDBSession.backend_name() == "LLDB"

    def test_gdb_name(self):
        assert GDBSession.backend_name() == "GDB"


# ---------------------------------------------------------------------------
# Init validation (shared across backends)
# ---------------------------------------------------------------------------

class TestInitValidation:
    @pytest.mark.parametrize("cls", [CDBSession, LLDBSession, GDBSession])
    def test_no_target_raises(self, cls):
        with pytest.raises((ValueError, TypeError)):
            cls()

    @pytest.mark.parametrize("cls", [CDBSession, LLDBSession, GDBSession])
    def test_missing_dump_raises(self, cls):
        with pytest.raises(FileNotFoundError):
            cls(dump_path="/nonexistent/test.dmp")


# ---------------------------------------------------------------------------
# get_local_dumps_path
# ---------------------------------------------------------------------------

class TestGetLocalDumpsPath:
    def test_cdb_returns_string_or_none(self):
        result = CDBSession.get_local_dumps_path()
        assert result is None or isinstance(result, str)

    def test_lldb_returns_string_or_none(self):
        result = LLDBSession.get_local_dumps_path()
        assert result is None or isinstance(result, str)

    def test_gdb_returns_string_or_none(self):
        result = GDBSession.get_local_dumps_path()
        assert result is None or isinstance(result, str)
