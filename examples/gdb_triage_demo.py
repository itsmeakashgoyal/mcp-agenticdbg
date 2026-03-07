#!/usr/bin/env python3
"""
Demonstration of enhanced GDB dump triage capabilities.

Shows both MI (machine interface) and CLI modes for robust crash analysis.
"""

import os
import sys

# Add parent to path for demo purposes
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from triagepilot.backends.gdb import GDBSession


def demo_cli_mode(dump_path: str):
    """Demonstrate CLI mode with marker-based command delimiting."""
    print("=" * 70)
    print("GDB CLI MODE (traditional text-based output)")
    print("=" * 70)
    
    with GDBSession(dump_path, use_mi=False, timeout=10) as session:
        print(f"\n✓ Session created for: {dump_path}")
        print(f"  Backend: {session.backend_name()}")
        
        # Get basic crash info
        print("\n📋 Crash Info:")
        info = session.get_crash_info()
        print(info[:500] if len(info) > 500 else info)
        
        # Get backtrace
        print("\n📚 Backtrace:")
        bt = session.get_stack_trace()
        print(bt[:500] if len(bt) > 500 else bt)
        
        # Get threads
        print("\n🧵 Threads:")
        threads = session.get_threads()
        print(threads[:300] if len(threads) > 300 else threads)


def demo_mi_mode(dump_path: str):
    """Demonstrate MI mode with structured output parsing."""
    print("\n" + "=" * 70)
    print("GDB MI MODE (machine interface for structured data)")
    print("=" * 70)
    
    with GDBSession(dump_path, use_mi=True, timeout=10) as session:
        print(f"\n✓ Session created for: {dump_path}")
        print(f"  Backend: {session.backend_name()}")
        
        # Get structured crash summary
        print("\n📊 Structured Crash Summary:")
        summary = session.get_crash_summary()
        
        print(f"  Signal: {summary.get('signal', 'N/A')}")
        print(f"  Registers: {len(summary.get('registers', {}))} captured")
        print(f"  Backtrace frames: {len(summary.get('backtrace', []))}")
        print(f"  Threads: {len(summary.get('threads', []))}")
        
        # Get specific variable (if symbol info is available)
        print("\n🔍 Variable Inspection (example):")
        var_value = session.get_variable("$pc")  # Program counter
        print(f"  $pc = {var_value}")
        
        # Execute console command through MI
        print("\n💻 Console command via MI:")
        output = session.send_command("info frame")
        print("  " + "\n  ".join(output[:5]))


def main():
    """Run the demo."""
    # Example: use README.md as a dummy "core" file for demo purposes
    # In real usage, you'd provide an actual core dump path
    demo_dump = os.path.join(
        os.path.dirname(__file__), 
        '..', 
        'README.md'
    )
    
    if not os.path.isfile(demo_dump):
        print(f"Error: Demo file not found: {demo_dump}")
        print("\nFor real usage, provide a path to an actual core dump:")
        print("  python gdb_triage_demo.py /path/to/core")
        return 1
    
    print("""
╔══════════════════════════════════════════════════════════════════════╗
║                  GDB Enhanced Dump Triage Demo                       ║
║                                                                      ║
║  This demo shows the dual-mode GDB backend:                         ║
║  • CLI mode: traditional text parsing with robust delimiting        ║
║  • MI mode: structured machine-readable output                      ║
║                                                                      ║
║  For dump triage, MI mode provides more reliable parsing.           ║
╚══════════════════════════════════════════════════════════════════════╝
""")
    
    # Note: These will fail with README.md as it's not a real core dump,
    # but they demonstrate the API.
    try:
        demo_cli_mode(demo_dump)
    except Exception as e:
        print(f"\n⚠️  CLI demo failed (expected with non-core file): {e}")
    
    try:
        demo_mi_mode(demo_dump)
    except Exception as e:
        print(f"\n⚠️  MI demo failed (expected with non-core file): {e}")
    
    print("\n" + "=" * 70)
    print("✅ Demo complete!")
    print("\nFor real crash dump analysis, use an actual core file:")
    print("  session = GDBSession('/var/crash/core.12345', use_mi=True)")
    print("  summary = session.get_crash_summary()")
    print("=" * 70)
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
