"""Live integration tests against a real lldb session.

Everywhere else these tests are skipped -- they only run in a dedicated CI
job (or a manual opt-in) that has:

- a real lldb executable reachable via DEFAULT_LLDB_PATHS or PATH
- TRIAGEPILOT_LIVE_LLDB_DUMP pointing at a real core file
- TRIAGEPILOT_LIVE_LLDB_BINARY pointing at the executable that produced it

They exist because the robustness fixes in backends/lldb.py -- unique
per-call completion markers, SIGINT recovery on timeout, and a kill()
fallback on shutdown -- only manifest against a real lldb subprocess with
real I/O timing. See each method's docstring in lldb.py for the bug it
addresses.

Run locally on macOS with a real core:

    export TRIAGEPILOT_LIVE_LLDB_DUMP=/path/to/core
    export TRIAGEPILOT_LIVE_LLDB_BINARY=/path/to/binary
    uv run pytest tests/test_lldb_live.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from triagepilot.backends.lldb import LLDBError, LLDBSession

DUMP_PATH = os.environ.get("TRIAGEPILOT_LIVE_LLDB_DUMP")
BINARY_PATH = os.environ.get("TRIAGEPILOT_LIVE_LLDB_BINARY")

_SKIP_REASON: str | None = None
if sys.platform == "win32":
    _SKIP_REASON = "LLDB live tests don't run on Windows"
elif not DUMP_PATH or not BINARY_PATH:
    _SKIP_REASON = "set TRIAGEPILOT_LIVE_LLDB_DUMP and TRIAGEPILOT_LIVE_LLDB_BINARY to run these"
elif not os.path.isfile(DUMP_PATH):
    _SKIP_REASON = f"TRIAGEPILOT_LIVE_LLDB_DUMP does not exist: {DUMP_PATH}"
elif not os.path.isfile(BINARY_PATH):
    _SKIP_REASON = f"TRIAGEPILOT_LIVE_LLDB_BINARY does not exist: {BINARY_PATH}"
elif not LLDBSession.find_debugger_executable():
    _SKIP_REASON = "no lldb found (checked DEFAULT_LLDB_PATHS and PATH)"

pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


def _count_lldb_processes() -> int:
    """Rough count of running lldb processes, used to detect leaks."""
    proc = subprocess.run(["pgrep", "-fc", "lldb"], capture_output=True, text=True)
    try:
        return int(proc.stdout.strip() or "0")
    except ValueError:
        return 0


class TestMarkerIsolation:
    def test_sequential_commands_do_not_bleed_output(self):
        """Distinct commands run back-to-back must not share output.

        Regression check for the fixed-marker bug: a stale echo from a
        previous command being mistaken for the current one's completion
        would show up here as identical or cross-contaminated output
        across unrelated commands.
        """
        session = LLDBSession(dump_path=DUMP_PATH, image_path=BINARY_PATH, timeout=30)
        try:
            status_out = "\n".join(session.send_command("process status", timeout=15))
            frame_out = "\n".join(session.send_command("frame info", timeout=15))
            reg_out = "\n".join(session.send_command("register read", timeout=15))

            assert status_out
            assert frame_out
            assert reg_out
            assert status_out != frame_out
            assert frame_out != reg_out
        finally:
            session.shutdown()


class TestTimeoutRecovery:
    def test_forced_timeout_keeps_session_usable(self):
        """A command given an impossible (0s) timeout must not wedge the session.

        Forces the SIGINT recovery path in _recover_from_timeout() by
        passing timeout=0 -- the client gives up before LLDB could possibly
        reply, regardless of how fast the command actually is. Recovery
        should still leave the session responsive for the next command.
        """
        session = LLDBSession(dump_path=DUMP_PATH, image_path=BINARY_PATH, timeout=30)
        try:
            with pytest.raises(LLDBError, match="timed out"):
                session.send_command("process status", timeout=0)

            # The actual regression check: before the SIGINT recovery fix,
            # LLDB was left mid-command and this call would time out too
            # (or return output mixing the two commands together).
            out = session.send_command("process status", timeout=20)
            assert out
        finally:
            session.shutdown()


class TestShutdownCleanup:
    def test_shutdown_leaves_no_orphaned_process(self):
        """shutdown() must not leave an lldb process running in the background.

        Regression check for the kill() fallback added to shutdown(): if
        terminate() doesn't work (some LLDB builds ignore SIGTERM while
        blocked in symbol I/O), the process must still end up dead rather
        than silently left running.
        """
        baseline = _count_lldb_processes()

        session = LLDBSession(dump_path=DUMP_PATH, image_path=BINARY_PATH, timeout=30)
        session.send_command("process status", timeout=15)
        session.shutdown()

        remaining = baseline + 1
        for _ in range(10):
            remaining = _count_lldb_processes()
            if remaining <= baseline:
                break
            time.sleep(0.5)

        assert remaining <= baseline, (
            "lldb process count did not return to baseline after shutdown() "
            "-- a process was likely leaked"
        )
