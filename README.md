# mcp-agenticdbg (TriagePilot)

Grounding AI debugging in runtime truth for crash dumps.

`mcp-agenticdbg` is an [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server that lets AI assistants triage crashes using real debugger output, not guesswork from logs alone.

Connect it to Cursor, VS Code, or any MCP-compatible client and ask:

- "What caused this crash?"
- "Show the call stack for this dump."
- "Find the faulting source line in my repo."

The assistant drives CDB/GDB/LLDB, extracts crash context, maps it to source, and optionally generates patch/PR artifacts.

Inspired by [`mcp-windbg`](https://github.com/svnscha/mcp-windbg).

## Platform Support

| Platform | Debugger | Dump Types | Status |
|----------|----------|------------|--------|
| Windows  | CDB / WinDbg | `.dmp` (minidump/full dump) | Supported |
| Linux    | GDB | `core`, `core.*`, `*.core` | Supported |
| macOS    | LLDB | core dumps | Supported |

Works with binaries compiled by **MSVC**, **Clang**, **GCC**, or any compiler that produces standard debug information.

## Quick Start

```bash
# Install (uv — recommended)
uv sync

# Or with pip
pip install -e .

# Verify
triagepilot --help

# Add MCP config (see Configuration below), then ask your assistant:
# "Analyze /path/to/crash.dmp and explain the root cause."
```

## How It Works

```text
You (Cursor / VS Code)
  |
  | "Why did this crash happen?"
  v
AI Assistant
  |
  v
TriagePilot MCP Server
  |
  +--> CDB (Windows) --> .dmp
  +--> GDB (Linux)   --> core dumps
  +--> LLDB (macOS)  --> core dumps
```

1. The AI calls TriagePilot's MCP tools (`analyze_dump`, `run_debugger_cmd`, etc.)
2. TriagePilot auto-detects your platform and launches the right debugger
3. The debugger analyzes the crash dump and returns structured results
4. TriagePilot locates the faulting source in your local repo
5. The AI explains the root cause, suggests fixes, and can create a PR

## Prerequisites

- **Python** `3.10+`
- **Debugger**: CDB/WinDbg (Windows), GDB (Linux), or LLDB (macOS)
- **MCP Client**: [Cursor](https://www.cursor.com/) or [VS Code](https://code.visualstudio.com/)

## Installation

```bash
git clone https://github.com/itsmeakashgoyal/mcp-agenticdbg.git
cd mcp-agenticdbg

# Using uv (recommended — fast, locked dependencies)
uv sync                          # Core deps + dev tools
uv sync --extra langgraph        # Optional: autonomous triage via LangGraph

# Or using pip
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
pip install -e .
pip install -e ".[langgraph]"    # Optional: LangGraph support
```

## Configuration

### Minimal MCP Config

```json
{
  "mcpServers": {
    "triagepilot": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "triagepilot"]
    }
  }
}
```

### With Symbols & Repo Path

```json
{
  "mcpServers": {
    "triagepilot": {
      "type": "stdio",
      "command": "python",
      "args": [
        "-m", "triagepilot",
        "--symbols-path", "/path/to/symbols",
        "--repo-path", "/path/to/repo"
      ]
    }
  }
}
```

Config file locations: `.cursor/mcp.json` (Cursor) or `.vscode/mcp.json` (VS Code).

## Available Tools

| Tool | Description |
|------|-------------|
| `analyze_dump` | One-shot crash analysis with stack/modules/threads/source lookup |
| `open_dump` | Open dump and initialize analysis session |
| `run_debugger_cmd` | Execute debugger command on active session |
| `send_ctrl_break` | Interrupt a running debugger command (CTRL+BREAK / SIGINT) |
| `close_dump` | Close active dump session |
| `list_dumps` | Discover dump files from platform-aware paths |
| `create_repo_pr` | Create commit + branch + push + GitHub PR |
| `create_shared_patch` | Generate markdown patch plan for shared/gitignored paths |
| `auto_triage_dump` | Autonomous end-to-end triage (requires `langgraph` extra) |
| `recall_similar_crashes` | Search memory for similar past crash analyses |
| `save_triage_result` | Save root cause and fix to persistent memory |
| `list_known_patterns` | Browse stored crash patterns |
| `forget_pattern` | Delete a memory entry by ID |

## CLI Options

```bash
triagepilot [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--debugger-type TYPE` | `auto` | Backend: `auto`, `cdb`, `lldb`, `gdb` |
| `--debugger-path PATH` | Auto-detected | Path to debugger executable |
| `--symbols-path PATH` | None | Symbol/debug info path |
| `--image-path PATH` | None | Executable image path |
| `--repo-path PATH` | None | Repository path for source lookup |
| `--timeout SECONDS` | `30` | Debugger command timeout |
| `--verbose` | Off | Enable debug-level logging |
| `--log-level LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

All options are also configurable via environment variables with the `TRIAGEPILOT_` prefix (e.g. `TRIAGEPILOT_DEBUGGER_TYPE=gdb`).

## Example Crash Programs

The `examples/` folder contains ten C++ programs that intentionally crash, covering stack overflow, use-after-free, double-free, vtable corruption, heap corruption, and more.

```bash
# Build
cd examples && ./build.sh          # Linux/macOS
cd examples && .\build.ps1         # Windows (MSVC)

# Generate core dump (macOS)
./gen_core_mac.sh use-after-free   # writes build/out/core.use-after-free
```

## Troubleshooting

See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) for common issues with debugger setup, core dump generation, symbol resolution, and more.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, code style, testing, and PR guidelines.

## License

BSD 3-Clause License. See [LICENSE](LICENSE) for details.
