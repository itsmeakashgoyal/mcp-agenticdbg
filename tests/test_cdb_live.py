"""Live integration tests against a real cdb.exe session.

Everywhere else these tests are skipped -- they only run in a dedicated CI
job (or a manual opt-in) that has:

- a real Windows machine with cdb.exe reachable via DEFAULT_CDB_PATHS or PATH
- an environment variable ``TRIAGEPILOT_LIVE_CDB_DUMP`` pointing at a real
  ``.dmp`` file to open

They exist because the robustness fixes in ``backends/cdb.py`` -- unique
per-call completion markers, CTRL+BREAK recovery on timeout, and
process-tree cleanup on shutdown -- only manifest against a real cdb.exe
subprocess with real I/O timing. A mocked unit test can assert the code
*calls* the right methods, but can't prove a stale marker doesn't bleed
into the next command's output, that CTRL+BREAK actually resynchronizes a
live session, or that a Store-alias-launched cdb.exe doesn't leave an
orphaned child behind. See each fixed method's docstring in cdb.py for the
bug it addresses.

Run locally on Windows with a real dump:

    set TRIAGEPILOT_LIVE_CDB_DUMP=C:\\path\\to\\some.dmp
    uv run pytest tests/test_cdb_live.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from triagepilot.backends.cdb import CDBError, CDBSession

DUMP_PATH = os.environ.get("TRIAGEPILOT_LIVE_CDB_DUMP")

_SKIP_REASON: str | None = None
if sys.platform != "win32":
    _SKIP_REASON = "CDB live tests only run on Windows"
elif not DUMP_PATH:
    _SKIP_REASON = "set TRIAGEPILOT_LIVE_CDB_DUMP to a real .dmp file to run these"
elif not os.path.isfile(DUMP_PATH):
    _SKIP_REASON = f"TRIAGEPILOT_LIVE_CDB_DUMP does not exist: {DUMP_PATH}"
elif not CDBSession.find_debugger_executable():
    _SKIP_REASON = "no cdb.exe found (checked DEFAULT_CDB_PATHS and PATH)"

pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


def _count_cdb_processes() -> int:
    """Rough count of running cdb*.exe processes, used to detect leaks.

    Matches both the Store execution alias (``cdbX64.exe`` etc.) and the
    real packaged binary it launches (``cdb.exe``).
    """
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq cdb*.exe"],
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    return out.lower().count(".exe")


class TestMarkerIsolation:
    def test_sequential_commands_do_not_bleed_output(self):
        """Distinct commands run back-to-back must not share output.

        Regression check for the fixed-marker bug: a stale echo from a
        previous command being mistaken for the current one's completion
        would show up here as identical or cross-contaminated output
        across unrelated commands.
        """
        session = CDBSession(dump_path=DUMP_PATH, timeout=30)
        try:
            version_out = "\n".join(session.send_command("version", timeout=15))
            lastevent_out = "\n".join(session.send_command(".lastevent", timeout=15))
            lm_out = "\n".join(session.send_command("lm", timeout=15))

            assert version_out
            assert lastevent_out
            assert lm_out
            assert version_out != lastevent_out
            assert lastevent_out != lm_out
        finally:
            session.shutdown()


class TestTimeoutRecovery:
    def test_forced_timeout_keeps_session_usable(self):
        """A command given an impossible (0s) timeout must not wedge the session.

        Forces the CTRL+BREAK recovery path in _recover_from_timeout() by
        passing timeout=0 -- the client gives up before CDB could possibly
        reply, regardless of how fast the command actually is. Recovery
        should still leave the session responsive for the next command.
        """
        session = CDBSession(dump_path=DUMP_PATH, timeout=30)
        try:
            with pytest.raises(CDBError, match="timed out"):
                session.send_command("version", timeout=0)

            # The actual regression check: before the CTRL+BREAK recovery
            # fix, CDB was left mid-command and this call would time out
            # too (or return output mixing the two commands together).
            out = session.send_command("version", timeout=20)
            assert out
            assert any("cdb" in line.lower() or "debugger" in line.lower() for line in out)
        finally:
            session.shutdown()


class TestShutdownCleanup:
    def test_shutdown_leaves_no_orphaned_process(self):
        """shutdown() must not leak a child cdb.exe (Store execution alias).

        Regression check for _kill_process_tree(): a plain terminate() on a
        cdb.exe launched via the Microsoft Store execution alias only kills
        the alias/launcher process, leaving the real debugger process
        (still holding the dump file open) running in the background.
        """
        baseline = _count_cdb_processes()

        session = CDBSession(dump_path=DUMP_PATH, timeout=30)
        session.send_command("version", timeout=15)
        session.shutdown()

        # Give the OS a moment to actually reap the process(es).
        remaining = baseline + 1
        for _ in range(10):
            remaining = _count_cdb_processes()
            if remaining <= baseline:
                break
            time.sleep(0.5)

        assert remaining <= baseline, (
            "cdb.exe process count did not return to baseline after shutdown() "
            "-- a child process was likely leaked"
        )
