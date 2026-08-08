"""Tests for the debugger command blocklist and rate limiter."""

import pytest
from mcp.shared.exceptions import McpError

from triagepilot.tools.debugger_tools import (
    _TokenBucket,
    validate_debugger_command,
)


class TestBlocklistPrefixMatching:
    def test_cdb_blocks_dangerous_prefix(self):
        with pytest.raises(McpError):
            validate_debugger_command(".shell calc.exe", "cdb")

    def test_gdb_blocks_shell(self):
        with pytest.raises(McpError):
            validate_debugger_command("shell whoami", "gdb")

    def test_lldb_blocks_platform_shell(self):
        with pytest.raises(McpError):
            validate_debugger_command("platform shell whoami", "lldb")

    def test_allows_ordinary_command(self):
        validate_debugger_command("bt full", "gdb")
        validate_debugger_command("r", "cdb")
        validate_debugger_command("print foo", "lldb")


class TestEmbeddedLineBreaks:
    """A "\\n" in the command lets it ride past the trailing echo marker
    every backend appends (see send_command() in each backends/*.py),
    effectively injecting a second, unvalidated command."""

    def test_rejects_embedded_newline_cdb(self):
        with pytest.raises(McpError):
            validate_debugger_command("r\n.shell calc.exe", "cdb")

    def test_rejects_embedded_newline_gdb(self):
        with pytest.raises(McpError):
            validate_debugger_command("bt\nshell whoami", "gdb")

    def test_rejects_embedded_carriage_return(self):
        with pytest.raises(McpError):
            validate_debugger_command("bt\rshell whoami", "gdb")


class TestCdbSemicolonChaining:
    """CDB treats ';' as a same-line command separator, so a blocked
    command can otherwise hide behind an allowed prefix."""

    def test_rejects_chained_blocked_command(self):
        with pytest.raises(McpError):
            validate_debugger_command("r;.shell whoami", "cdb")

    def test_rejects_chained_blocked_command_with_spaces(self):
        with pytest.raises(McpError):
            validate_debugger_command("r ; .kill", "cdb")

    def test_semicolons_only_special_cased_for_cdb(self):
        # gdb/lldb don't treat ';' as a chaining operator, so it must not
        # trigger the per-segment split (and must not itself be rejected).
        validate_debugger_command("bt; info registers", "gdb")


class TestExtraCodeExecutionPrimitives:
    """GDB/LLDB expose code-execution primitives beyond a raw shell."""

    @pytest.mark.parametrize(
        "command",
        ["python import os; os.system('whoami')", "pi print(1)", 'call system("whoami")'],
    )
    def test_gdb_blocks_code_exec_primitives(self, command):
        with pytest.raises(McpError):
            validate_debugger_command(command, "gdb")

    def test_lldb_blocks_script(self):
        with pytest.raises(McpError):
            validate_debugger_command("script import os", "lldb")

    def test_lldb_still_allows_expression_and_print(self):
        # Documented in dump-triage.prompt.md as the LLDB way to evaluate
        # expressions; must not be caught by the "script" block.
        validate_debugger_command("expression foo", "lldb")
        validate_debugger_command("print foo", "lldb")


class TestTokenBucket:
    def test_allows_burst_up_to_capacity(self):
        bucket = _TokenBucket(rate=10.0, capacity=3.0)
        assert bucket.consume() is True
        assert bucket.consume() is True
        assert bucket.consume() is True
        assert bucket.consume() is False

    def test_refills_over_time(self, monkeypatch):
        import triagepilot.tools.debugger_tools as debugger_tools_module

        now = [1000.0]
        monkeypatch.setattr(debugger_tools_module.time, "monotonic", lambda: now[0])

        bucket = _TokenBucket(rate=10.0, capacity=1.0)
        assert bucket.consume() is True
        assert bucket.consume() is False

        now[0] += 0.5  # 0.5s at rate=10/s refills 5 tokens, capped at capacity
        assert bucket.consume() is True
