"""Tests for v0.13.0-inspired features: stale session cleanup, send_break, dump patterns, config."""

import os
import signal
import sys
from unittest.mock import MagicMock, patch

import pytest

from triagepilot.backends.base import DebuggerSession
from triagepilot.tools.debugger_tools import (
    _dump_file_patterns,
    _evict_lru_session,
    _session_lock,
    active_sessions,
    cleanup_all_sessions,
    handle_send_break,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeSession(DebuggerSession):
    """Minimal concrete DebuggerSession for testing."""

    def __init__(self, dump_path="/fake/dump.dmp"):
        self.dump_path = dump_path
        self.symbols_path = None
        self.image_path = None
        self.timeout = 10
        self.verbose = False
        self.process = MagicMock()
        self.process.poll.return_value = None  # process is running

    def send_command(self, command, timeout=None):
        return ["ok"]

    def shutdown(self):
        pass

    def get_session_id(self):
        return os.path.abspath(self.dump_path)

    def _analysis_command(self):
        return "analyze"

    def _crash_info_command(self):
        return "info"

    def _stack_trace_command(self):
        return "bt"

    def _modules_command(self):
        return "modules"

    def _threads_command(self):
        return "threads"

    @staticmethod
    def get_local_dumps_path():
        return None

    @staticmethod
    def find_debugger_executable(custom_path=None):
        return "/fake/debugger"

    @staticmethod
    def backend_name():
        return "Fake"


# ---------------------------------------------------------------------------
# Stale session cleanup hardening
# ---------------------------------------------------------------------------


class TestStaleSessionCleanup:
    def setup_method(self):
        """Clear active_sessions before each test."""
        active_sessions.clear()

    def test_evict_lru_removes_session_on_shutdown_failure(self):
        """Session must be removed from pool even if shutdown() raises."""
        session = _FakeSession()
        session.shutdown = MagicMock(side_effect=RuntimeError("boom"))
        session_id = "/fake/dump.dmp"
        active_sessions[session_id] = session

        with _session_lock:
            _evict_lru_session()

        assert session_id not in active_sessions
        session.shutdown.assert_called_once()

    def test_evict_lru_removes_session_on_success(self):
        session = _FakeSession()
        session_id = "/fake/dump.dmp"
        active_sessions[session_id] = session

        with _session_lock:
            _evict_lru_session()

        assert session_id not in active_sessions

    def test_cleanup_all_removes_on_shutdown_failure(self):
        """All sessions must be removed even if some shutdown() calls fail."""
        s1 = _FakeSession("/fake/1.dmp")
        s1.shutdown = MagicMock(side_effect=RuntimeError("fail"))
        s2 = _FakeSession("/fake/2.dmp")
        s2.shutdown = MagicMock()

        active_sessions["/fake/1.dmp"] = s1
        active_sessions["/fake/2.dmp"] = s2

        cleanup_all_sessions()

        assert len(active_sessions) == 0
        s1.shutdown.assert_called_once()
        s2.shutdown.assert_called_once()

    def test_cleanup_all_empty(self):
        """No-op on empty pool."""
        cleanup_all_sessions()
        assert len(active_sessions) == 0


# ---------------------------------------------------------------------------
# send_break on base class
# ---------------------------------------------------------------------------


class TestSendBreakBase:
    def test_default_raises_not_implemented(self):
        """Base DebuggerSession.send_break raises NotImplementedError."""
        session = _FakeSession()
        # Call the base class method directly (our _FakeSession doesn't override it)
        with pytest.raises(NotImplementedError, match="Fake"):
            DebuggerSession.send_break(session)


# ---------------------------------------------------------------------------
# send_break on CDB backend
# ---------------------------------------------------------------------------


class TestSendBreakCDB:
    def test_send_break_unix(self):
        """CDB send_break uses SIGINT on non-Windows platforms."""
        from triagepilot.backends.cdb import CDBSession

        session = CDBSession.__new__(CDBSession)
        session.process = MagicMock()
        session.process.poll.return_value = None

        with patch.object(sys, "platform", "linux"):
            result = session.send_break()

        assert result is True
        session.process.send_signal.assert_called_once_with(signal.SIGINT)

    def test_send_break_process_dead(self):
        from triagepilot.backends.cdb import CDBSession

        session = CDBSession.__new__(CDBSession)
        session.process = MagicMock()
        session.process.poll.return_value = 1  # already exited

        assert session.send_break() is False


# ---------------------------------------------------------------------------
# send_break on GDB backend
# ---------------------------------------------------------------------------


class TestSendBreakGDB:
    def test_send_break(self):
        from triagepilot.backends.gdb import GDBSession

        session = GDBSession.__new__(GDBSession)
        session.process = MagicMock()
        session.process.poll.return_value = None

        result = session.send_break()
        assert result is True
        session.process.send_signal.assert_called_once_with(signal.SIGINT)

    def test_send_break_no_process(self):
        from triagepilot.backends.gdb import GDBSession

        session = GDBSession.__new__(GDBSession)
        session.process = None

        assert session.send_break() is False


# ---------------------------------------------------------------------------
# send_break on LLDB backend
# ---------------------------------------------------------------------------


class TestSendBreakLLDB:
    def test_send_break(self):
        from triagepilot.backends.lldb import LLDBSession

        session = LLDBSession.__new__(LLDBSession)
        session.process = MagicMock()
        session.process.poll.return_value = None

        result = session.send_break()
        assert result is True
        session.process.send_signal.assert_called_once_with(signal.SIGINT)


# ---------------------------------------------------------------------------
# handle_send_break MCP handler
# ---------------------------------------------------------------------------


class TestHandleSendBreak:
    def setup_method(self):
        active_sessions.clear()

    @pytest.mark.asyncio
    async def test_send_break_success(self):
        from pydantic import BaseModel, Field

        class SendBreakParams(BaseModel):
            dump_path: str = Field()

        session = _FakeSession("/fake/dump.dmp")
        session.send_break = MagicMock(return_value=True)
        abs_path = os.path.abspath("/fake/dump.dmp")
        active_sessions[abs_path] = session

        result = await handle_send_break(
            {"dump_path": "/fake/dump.dmp"}, SendBreakParams=SendBreakParams
        )

        assert len(result) == 1
        assert "Break signal sent" in result[0].text
        session.send_break.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_break_no_session(self):
        from mcp.shared.exceptions import McpError
        from pydantic import BaseModel, Field

        class SendBreakParams(BaseModel):
            dump_path: str = Field()

        with pytest.raises(McpError, match="No active session"):
            await handle_send_break(
                {"dump_path": "/nonexistent/dump.dmp"}, SendBreakParams=SendBreakParams
            )


# ---------------------------------------------------------------------------
# Dump file patterns
# ---------------------------------------------------------------------------


class TestDumpFilePatterns:
    def test_cdb_includes_cab(self):
        patterns = _dump_file_patterns("cdb")
        assert "*.cab" in patterns
        assert "*.*dmp" in patterns

    def test_lldb_darwin(self):
        with patch.object(sys, "platform", "darwin"):
            patterns = _dump_file_patterns("lldb")
            assert "*.crash" in patterns

    def test_gdb_core(self):
        patterns = _dump_file_patterns("gdb")
        assert "core.*" in patterns


