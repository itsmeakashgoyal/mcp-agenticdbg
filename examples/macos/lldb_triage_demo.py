#!/usr/bin/env python3
"""
Demonstration of LLDB-backed crash dump triage on macOS (or Linux with LLDB).

Usage:
    # Generate a core dump first (see below), then:
    cd examples/macos
    python lldb_triage_demo.py build/out/core.use-after-free \
        --image build/out/use-after-free

Generating a core dump on macOS:
    # Use gen_core_mac.sh — direct execution sends crashes to DiagnosticReports,
    # not a binary core file, on macOS 12+.
    cd examples/macos
    ./gen_core_mac.sh use-after-free          # -> build/out/core.use-after-free
    python lldb_triage_demo.py build/out/core.use-after-free \
        --image build/out/use-after-free
"""

import argparse
import os
import sys

# Allow running from the repo root without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from triagepilot.backends.lldb import LLDBSession, LLDBError


def _section(title: str, content: str, max_chars: int = 800) -> None:
    print(f"\n{'='*70}")
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
    parser = argparse.ArgumentParser(description="LLDB triage demo for macOS")
    parser.add_argument("core", help="Path to core dump file")
    parser.add_argument(
        "--image",
        default=None,
        help="Path to the crashed executable (improves symbol resolution)",
    )
    parser.add_argument(
        "--lldb",
        default=None,
        help="Path to lldb binary (auto-detected if omitted)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=15,
        help="Per-command timeout in seconds (default: 15)",
    )
    args = parser.parse_args()

    print(f"""
╔══════════════════════════════════════════════════════════════════════╗
║                  LLDB Crash Dump Triage Demo                         ║
║                                                                      ║
║  Core  : {args.core[:60]:<60}║
║  Image : {(args.image or 'auto')[:60]:<60}║
╚══════════════════════════════════════════════════════════════════════╝
""")

    try:
        session = LLDBSession(
            dump_path=args.core,
            debugger_path=args.lldb,
            image_path=args.image,
            timeout=args.timeout,
            verbose=False,
        )
    except (LLDBError, FileNotFoundError, ValueError) as e:
        print(f"ERROR: Failed to open session: {e}")
        return 1

    with session:
        print(f"Session opened  (backend: {session.backend_name()})")
        print(f"LLDB path      : {session.debugger_path}")

        # 1. Structured crash summary
        summary = session.get_crash_summary()
        _section("Process Status / Signal", summary.get("signal") or "")
        _section("Crash Frame", "\n".join(summary.get("crash_frame") or []))
        _section("Stack Trace (crashing thread)", "\n".join(summary.get("backtrace") or []))

        # 2. All threads
        threads = session.get_thread_backtraces()
        print(f"\n  Threads found: {len(threads)}")
        for i, t in enumerate(threads[:3]):  # show first 3 threads
            _section(f"Thread {i+1}: {t.get('id', '?')}", t.get("raw", ""))

        # 3. Registers
        _section("Registers", session.get_all_registers(), max_chars=1200)

        # 4. Frame locals (frame 0)
        locals_info = session.get_frame_locals(0)
        _section("Frame 0 Locals", locals_info.get("raw") or "")

        # 5. Disassembly around crash PC
        _section("Disassembly (crash PC ±30 instructions)", session.get_disassembly())

        # 6. Loaded images
        _section("Loaded Images", session.get_inferior_info(), max_chars=1000)

        # 7. Full rich analysis (what analyze_dump uses)
        print("\n" + "=" * 70)
        print("  Full run_crash_analysis() output (first 2000 chars)")
        print("=" * 70)
        full = session.run_crash_analysis()
        print(full[:2000])
        if len(full) > 2000:
            print(f"\n  ... [{len(full) - 2000} chars truncated]")

    print("\n" + "=" * 70)
    print("  Demo complete.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
