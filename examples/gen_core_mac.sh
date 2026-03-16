#!/bin/bash
# Generate a core dump from an example crash program on macOS.
#
# Uses lldb to run the binary so that lldb intercepts the crash before
# macOS's ReportCrash agent does, then saves the core via
# `process save-core`.  This bypasses the /cores permission issues and
# the ReportCrash interception that plague `ulimit -c unlimited` on
# modern macOS.
#
# Usage:
#   cd examples
#   chmod +x gen_core_mac.sh
#   ./gen_core_mac.sh [example-name] [output-core-path]
#
# Examples:
#   ./gen_core_mac.sh use-after-free
#   ./gen_core_mac.sh stack-overflow /tmp/stack.core
#
# The core dump path defaults to build/out/core.<example-name>

set -euo pipefail

OUTDIR="build/out"
EXAMPLE="${1:-use-after-free}"
BIN="$OUTDIR/$EXAMPLE"
CORE="${2:-$OUTDIR/core.$EXAMPLE}"

if [[ ! -f "$BIN" ]]; then
    echo "ERROR: Binary not found: $BIN"
    echo "Run ./build.sh first to compile the examples."
    exit 1
fi

LLDB=$(command -v lldb 2>/dev/null || true)
if [[ -z "$LLDB" ]]; then
    echo "ERROR: lldb not found in PATH."
    echo "Install Xcode Command Line Tools:  xcode-select --install"
    exit 1
fi

echo "Running $BIN under lldb ..."
echo "(lldb will intercept the crash and save a core to $CORE)"
echo ""

# Run lldb in batch mode:
#   -o "run"                  – start the process
#   --one-line-on-crash       – these commands run only when the process stops
#                               due to a crash signal (EXC_BAD_ACCESS, etc.)
#   process save-core $CORE  – write the core at the crash point
#   quit                     – exit lldb
#
# Note: plain `-o "process save-core"` after `-o run` is NOT executed after a
# crash in batch mode (lldb exits the run command loop).  --one-line-on-crash
# is the correct hook for post-crash commands.
"$LLDB" "$BIN" \
    --batch \
    -o "run" \
    --one-line-on-crash "process save-core $CORE" \
    --one-line-on-crash "quit" \
    2>&1 || true

if [[ -f "$CORE" ]]; then
    echo ""
    echo "Core dump written to: $CORE"
    echo ""
    echo "Analyze with triagepilot:"
    echo "  python examples/lldb_triage_demo.py $CORE --image $(pwd)/$BIN"
    echo ""
    echo "Or via MCP tools:"
    echo "  analyze_dump $CORE"
else
    echo ""
    echo "WARNING: Core dump not found at $CORE"
    echo ""
    echo "Troubleshooting:"
    echo "  1. Confirm lldb can run the binary:  lldb $BIN -o run -o quit"
    echo "  2. Check disk space:  df -h ."
    echo "  3. Try an explicit output path:  $0 $EXAMPLE /tmp/$EXAMPLE.core"
    exit 1
fi
