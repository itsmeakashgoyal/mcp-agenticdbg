"""Live integration tests against a real gdb session.

Everywhere else these tests are skipped -- they only run in a dedicated CI
job (or a manual opt-in) that has:

- a real gdb executable reachable via DEFAULT_GDB_PATHS or PATH
- TRIAGEPILOT_LIVE_GDB_DUMP pointing at a real core file
- TRIAGEPILOT_LIVE_GDB_BINARY pointing at the executable that produced it

They exist because the CLI-fallback-mode fixes in backends/gdb.py --
unique per-call completion markers and SIGINT recovery on timeout -- only
manifest against a real gdb subprocess with real I/O timing. MI mode
(the default) is not vulnerable to the marker-collision bug (its
per-token routing already isolates responses), so this file also checks
that MI mode's SIGINT-on-timeout addition doesn't itself break anything.
See _recover_cli_from_timeout() / _send_mi_command() in gdb.py for the
rationale behind each.

Run locally on Linux with a real core:

    export TRIAGEPILOT_LIVE_GDB_DUMP=/path/to/core
    export TRIAGEPILOT_LIVE_GDB_BINARY=/path/to/binary
    uv run pytest tests/test_gdb_live.py -v
"""

from __future__ import annotations

import os
import sys

import pytest

from triagepilot.backends.gdb import GDBError, GDBSession

DUMP_PATH = os.environ.get("TRIAGEPILOT_LIVE_GDB_DUMP")
BINARY_PATH = os.environ.get("TRIAGEPILOT_LIVE_GDB_BINARY")

_SKIP_REASON: str | None = None
if sys.platform == "win32":
    _SKIP_REASON = "GDB live tests don't run on Windows"
elif not DUMP_PATH or not BINARY_PATH:
    _SKIP_REASON = "set TRIAGEPILOT_LIVE_GDB_DUMP and TRIAGEPILOT_LIVE_GDB_BINARY to run these"
elif not os.path.isfile(DUMP_PATH):
    _SKIP_REASON = f"TRIAGEPILOT_LIVE_GDB_DUMP does not exist: {DUMP_PATH}"
elif not os.path.isfile(BINARY_PATH):
    _SKIP_REASON = f"TRIAGEPILOT_LIVE_GDB_BINARY does not exist: {BINARY_PATH}"
elif not GDBSession.find_debugger_executable():
    _SKIP_REASON = "no gdb found (checked DEFAULT_GDB_PATHS and PATH)"

pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


class TestCLIMarkerIsolation:
    def test_sequential_commands_do_not_bleed_output(self):
        """Distinct commands run back-to-back must not share output.

        Regression check for CLI mode's fixed-marker bug (see
        _dispatch_cli_line()): a stale echo from a previous command being
        mistaken for the current one's completion would show up here as
        identical or cross-contaminated output across unrelated commands.
        """
        session = GDBSession(
            dump_path=DUMP_PATH, image_path=BINARY_PATH, timeout=30, use_mi=False
        )
        try:
            info_out = "\n".join(session.send_command("info signal", timeout=15))
            bt_out = "\n".join(session.send_command("bt 5", timeout=15))
            reg_out = "\n".join(session.send_command("info registers", timeout=15))

            assert info_out
            assert bt_out
            assert reg_out
            assert info_out != bt_out
            assert bt_out != reg_out
        finally:
            session.shutdown()


class TestCLITimeoutRecovery:
    def test_forced_timeout_keeps_session_usable(self):
        """A command given an impossible (0s) timeout must not wedge the session.

        Forces the SIGINT recovery path in _recover_cli_from_timeout() by
        passing timeout=0 -- the client gives up before GDB could possibly
        reply, regardless of how fast the command actually is. Recovery
        should still leave the session responsive for the next command.
        """
        session = GDBSession(
            dump_path=DUMP_PATH, image_path=BINARY_PATH, timeout=30, use_mi=False
        )
        try:
            with pytest.raises(GDBError, match="timed out"):
                session.send_command("info signal", timeout=0)

            # The actual regression check: before the SIGINT recovery fix,
            # GDB was left mid-command and this call would time out too (or
            # return output mixing the two commands together).
            out = session.send_command("info signal", timeout=20)
            assert out
        finally:
            session.shutdown()


class TestMITimeoutHandling:
    def test_forced_timeout_does_not_break_later_commands(self):
        """MI mode's SIGINT-on-timeout addition must not itself regress anything.

        MI's per-token routing already makes a forced timeout harmless (a
        late result for an abandoned token is just dropped), so this isn't
        a correctness regression check the way the CLI-mode one above is --
        it just confirms the added send_break() call in _send_mi_command()
        doesn't break the session for the next command.
        """
        session = GDBSession(dump_path=DUMP_PATH, image_path=BINARY_PATH, timeout=30, use_mi=True)
        try:
            with pytest.raises(GDBError, match="timed out"):
                session.send_command("info signal", timeout=0)

            out = session.send_command("info signal", timeout=20)
            assert out
        finally:
            session.shutdown()
