#!/usr/bin/env python3
"""
Standalone GDB crash dump triage demo for Linux.

Usage:
    # Generate a core dump first:
    cd examples/linux
    ulimit -c unlimited
    ./build/out/use-after-free          # crashes -> core.<pid>

    # Then run the demo:
    python gdb_triage_demo.py core.use-after-free.1234 \
        --image build/out/use-after-free

    # GDB dual-mode: MI (structured) by default, --cli for text mode
    python gdb_triage_demo.py core.1234 --image build/out/use-after-free --cli
"""

import argparse
import os
import sys

# Allow running from the repo root without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from triagepilot.backends.gdb import GDBSession, GDBError


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
    parser = argparse.ArgumentParser(description="GDB triage demo for Linux")
    parser.add_argument("core", help="Path to core dump file")
    parser.add_argument(
        "--image",
        default=None,
        help="Path to the crashed executable (improves symbol resolution)",
    )
    parser.add_argument(
        "--gdb",
        default=None,
        help="Path to gdb binary (auto-detected if omitted)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=15,
        help="Per-command timeout in seconds (default: 15)",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Use CLI mode instead of MI (machine interface) mode",
    )
    args = parser.parse_args()

    use_mi = not args.cli
    mode_label = "MI (machine interface)" if use_mi else "CLI (text-based)"

    print(f"""
======================================================================
  GDB Crash Dump Triage Demo

  Core  : {args.core}
  Image : {args.image or 'auto'}
  Mode  : {mode_label}
======================================================================
""")

    try:
        session = GDBSession(
            dump_path=args.core,
            debugger_path=args.gdb,
            image_path=args.image,
            timeout=args.timeout,
            use_mi=use_mi,
            verbose=False,
        )
    except (GDBError, FileNotFoundError, ValueError) as e:
        print(f"ERROR: Failed to open session: {e}")
        return 1

    with session:
        print(f"Session opened  (backend: {session.backend_name()}, mode: {mode_label})")
        print(f"GDB path       : {session.debugger_path}")

        # 1. Structured crash summary
        summary = session.get_crash_summary()
        _section("Signal / Crash Reason", summary.get("signal") or "")
        _section("Crash Frame", "\n".join(summary.get("crash_frame") or []))
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
        _section("Frame 0 Locals", locals_info.get("raw") or "")

        # 5. Disassembly around crash PC
        _section("Disassembly (crash PC)", session.get_disassembly())

        # 6. Inferior info
        _section("Inferior / Binary Info", session.get_inferior_info(), max_chars=1000)

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
