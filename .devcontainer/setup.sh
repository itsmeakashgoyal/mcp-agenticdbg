#!/bin/bash
# One-time Codespaces/devcontainer setup: installs gdb + a C++ toolchain,
# installs TriagePilot, builds the example crash programs, and generates one
# ready-to-analyze core dump so there's something to try immediately.
set -eo pipefail

echo "==> Installing gdb and a C++ toolchain"
sudo apt-get update -qq
sudo apt-get install -y -qq gdb build-essential

echo "==> Installing uv"
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

echo "==> Installing TriagePilot"
uv sync

echo "==> Building example crash programs"
(cd examples/linux && bash build.sh)

echo "==> Generating a sample core dump (use-after-free)"
echo core | sudo tee /proc/sys/kernel/core_pattern > /dev/null
(
    cd examples/linux/build/out
    ulimit -c unlimited
    ./use-after-free || true
)
# core_uses_pid determines whether the file is "core" or "core.<pid>" --
# don't assume, find whatever actually got written.
CORE_FILE=$(find examples/linux/build/out -maxdepth 1 -name 'core*' -newer examples/linux/build/out/use-after-free | head -1)

cat <<EOF

TriagePilot is ready.
EOF
if [[ -n "$CORE_FILE" ]]; then
    cat <<EOF
A sample crash is waiting at $CORE_FILE

Try the eval harness against it directly:
  uv run python eval/run_eval.py --only use-after-free

Or open it in TriagePilot's own demo script:
  uv run python examples/linux/gdb_triage_demo.py $CORE_FILE --image examples/linux/build/out/use-after-free
EOF
else
    cat <<'EOF'
No core dump was produced automatically (core_pattern may be locked down
in this environment) -- generate one yourself:
  cd examples/linux/build/out && ulimit -c unlimited && ./use-after-free
EOF
fi
cat <<'EOF'

Start the MCP server and point an MCP-compatible client (Cursor, VS Code,
Claude Code) at it:
  uv run triagepilot --debugger gdb --repo-path .

See README.md's "Quick Start" for client configuration.
EOF
