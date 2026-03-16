#!/usr/bin/env python3
"""
Standalone CDB crash dump triage demo for Windows.

Usage:
    # Generate dumps first:
    cd examples\\windows
    .\\build.ps1
    .\\run-all.ps1 -Name use-after-free

    # Then run the demo:
    python cdb_triage_demo.py build\\out\\dumps\\use-after-free.exe.1234.dmp

    # With explicit symbols and image paths:
    python cdb_triage_demo.py build\\out\\dumps\\use-after-free.exe.1234.dmp ^
        --symbols build\\out --image build\\out
"""

import argparse
import os
import sys

# Allow running from the repo root without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from triagepilot.backends.cdb import CDBSession, CDBError


def _section(title: str, content: str, max_chars: int = 800) -> None:
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print("=" * 70)
    if content:
        trimmed = content[:max_chars]
        if len(content) > max_chars:
            trimmed += f"\n  ... [{len(content) - max_chars} chars truncated]"
        print(trimmed)
    else:
        print("  (no output)")


def main() -> int:
    parser = argparse.ArgumentParser(description="CDB triage demo for Windows")
    parser.add_argument("dump", help="Path to .dmp crash dump file")
    parser.add_argument(
        "--symbols",
        default=None,
        help="Path to symbols directory (folder containing .pdb files)",
    )
    parser.add_argument(
        "--image",
        default=None,
        help="Path to image directory (folder containing .exe/.dll files)",
    )
    parser.add_argument(
        "--cdb",
        default=None,
        help="Path to cdb.exe (auto-detected if omitted)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Per-command timeout in seconds (default: 30)",
    )
    args = parser.parse_args()

    print(f"""
======================================================================
  CDB Crash Dump Triage Demo

  Dump    : {args.dump}
  Symbols : {args.symbols or 'auto'}
  Image   : {args.image or 'auto'}
======================================================================
""")

    try:
        session = CDBSession(
            dump_path=args.dump,
            debugger_path=args.cdb,
            symbols_path=args.symbols,
            image_path=args.image,
            timeout=args.timeout,
            verbose=False,
        )
    except (CDBError, FileNotFoundError, ValueError) as e:
        print(f"ERROR: Failed to open session: {e}")
        return 1

    with session:
        print(f"Session opened  (backend: {session.backend_name()})")
        print(f"CDB path       : {session.debugger_path}")

        # 1. Structured crash summary
        summary = session.get_crash_summary()
        _section("Last Event / Signal", summary.get("signal") or "")
        _section("Exception Context (.ecxr)", "\n".join(summary.get("crash_frame") or []))
        _section("Stack Trace (crashing thread)", "\n".join(summary.get("backtrace") or []))

        # 2. All threads
        threads = session.get_thread_backtraces()
        print(f"\n  Threads found: {len(threads)}")
        for i, t in enumerate(threads[:3]):  # show first 3 threads
            _section(f"Thread {i + 1}: {t.get('id', '?')}", t.get("raw", ""))

        # 3. Registers
        _section("Registers", session.get_all_registers(), max_chars=1200)

        # 4. Frame locals (frame 0)
        locals_info = session.get_frame_locals(0)
        _section("Frame 0 Locals (dv /t)", locals_info.get("raw") or "")

        # 5. Disassembly around crash PC
        _section("Disassembly (crash PC)", session.get_disassembly())

        # 6. Target and process info
        _section("Target / Process Info", session.get_inferior_info(), max_chars=1000)

        # 7. Full rich analysis (what analyze_dump uses)
        print(f"\n{'=' * 70}")
        print("  Full run_crash_analysis() output (first 2000 chars)")
        print("=" * 70)
        full = session.run_crash_analysis()
        print(full[:2000])
        if len(full) > 2000:
            print(f"\n  ... [{len(full) - 2000} chars truncated]")

    print(f"\n{'=' * 70}")
    print("  Demo complete.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
