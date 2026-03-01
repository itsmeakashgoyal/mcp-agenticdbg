# mcp-agenticdbg (TriagePilot)

Grounding AI debugging in runtime truth for crash dumps.

`mcp-agenticdbg` is an [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server `triagepilot` that lets AI assistants triage crashes using real debugger output, not guesswork from logs alone.

Connect it to Cursor, VS Code, or any MCP-compatible client and ask:

- "What caused this crash?"
- "Show the call stack for this dump."
- "Find the faulting source line in my repo."

The assistant can drive CDB/GDB/LLDB, extract crash context, map it to source, and optionally generate patch/PR artifacts.

Inspired by [`mcp-windbg`](https://github.com/svnscha/mcp-windbg).

## Key Insight

Most engineering time is not spent writing code. It is spent understanding failures.

AI coding assistants can write code fast, but when production crashes happen they still need runtime truth to explain:

- what happened
- where it happened
- why it happened

Without debugger-grounded execution context, AI triage loops on guess, add logs, rerun, repeat.

## The Problem in AI-Augmented Debugging

- Logs only show what you predicted you would need.
- Postmortems without stack/module/thread context are slow.
- Large/legacy codebases make manual fault localization expensive.
- "Plausible" AI answers are risky when not grounded in debugger output.

## The Solution: Agentic Crash-Dump Debugging

TriagePilot gives your AI assistant controlled access to platform-native debuggers and crash triage tools over MCP.

That means your assistant can move from "clever guesser" to "grounded debugger":

1. Open dump/core/crash artifacts.
2. Run analysis commands.
3. Extract stack, module, thread, and crash metadata.
4. Locate likely faulting source in your repository.
5. Return root-cause-oriented explanations and next steps.

## Current Status

| Platform | Debugger | Dump Types | Status |
| --- | --- | --- | --- |
| Windows | CDB / WinDbg | `.dmp` (minidump/full dump) | Supported |
| Linux | GDB | `core`, `core.*`, `*.core` | In progress |
| macOS | LLDB | `.crash`, `.ips`, core dumps | In progress |

Linux and macOS paths are implemented but still evolving. Treat them as in-progress compared with Windows.
Works with binaries compiled by **MSVC**, **Clang**, **GCC**, or any compiler that produces standard debug information

## Table of Contents

- [Quick Start](#quick-start)
- [How It Works](#how-it-works)
- [Why It Stands Out](#why-it-stands-out)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Available Tools](#available-tools)
- [CLI Options](#cli-options)
- [Environment Variables](#environment-variables)
- [Example Crash Programs](#example-crash-programs)
- [Safety Model](#safety-model)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)

## Quick Start

```bash
# 1) Install
pip install -e .

# 2) Verify
triagepilot --help

# 3) Add MCP config (Cursor/VS Code)
# see Configuration section

# 4) Ask your assistant
"Analyze /path/to/crash.dmp and explain the root cause."
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
  +--> LLDB (macOS)  --> .crash / .ips / core dumps
```

Flow:
TriagePilot acts as a bridge between your AI assistant and the platform-native debugger. When you ask the AI to analyze a crash:

1. **The AI calls TriagePilot's MCP tools** (`analyze_dump`, `run_debugger_cmd`, etc.)
2. **TriagePilot auto-detects your platform** and launches the right debugger (CDB on Windows, GDB on Linux, LLDB on macOS)
3. **The debugger analyzes the crash dump** and returns structured results (stack traces, modules, crash info)
4. **TriagePilot locates the faulting source** in your local repo using a multi-level search (debug info -> symbol name -> stack trace)
5. **The AI interprets the results** and explains the root cause, suggests fixes, and can create a PR

## Why It Stands Out

- Runtime-grounded crash analysis over MCP.
- Source-aware fault localization from debugger symbols/frames.
- One-shot and session-based workflows (`analyze_dump` or `open_dump` + iterative commands).
- Optional autonomous graph-based flow (`auto_triage_dump`) when LangGraph extra is installed.
- Practical delivery tooling: create patch markdown or repo PR from results.

## Prerequisites

### Python

- Python `3.10+`

```bash
python --version
```

### Debugger (platform dependent)

#### Windows: CDB / WinDbg

Install via:

- [WinDbg (Microsoft Store)](https://apps.microsoft.com/detail/9pgjgd53tn86)
- [Windows SDK](https://developer.microsoft.com/en-us/windows/downloads/windows-sdk/) (`Debugging Tools for Windows`)

#### Linux: GDB

```bash
# Debian / Ubuntu
sudo apt install gdb

# Fedora / RHEL
sudo dnf install gdb
```

Enable core dumps:

```bash
ulimit -c unlimited
```

#### macOS: LLDB

```bash
xcode-select --install
```

### MCP Client

- [Cursor](https://www.cursor.com/)
- [VS Code](https://code.visualstudio.com/) with MCP support

## Installation

```bash
git clone https://github.com/itsmeakashgoyal/mcp-agenticdbg.git
cd mcp-agenticdbg

python -m venv .venv
source .venv/bin/activate   # Windows PowerShell: .venv\Scripts\Activate.ps1

pip install -e .
```

### Optional: LangGraph Autonomous Triage

```bash
pip install -e ".[langgraph]"
```

This enables the `auto_triage_dump` tool.

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

### Windows + Microsoft Symbol Server

```json
{
  "mcpServers": {
    "triagepilot": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "triagepilot"],
      "env": {
        "_NT_SYMBOL_PATH": "SRV*C:\\Symbols*https://msdl.microsoft.com/download/symbols"
      }
    }
  }
}
```

### Custom Args

```json
{
  "mcpServers": {
    "triagepilot": {
      "type": "stdio",
      "command": "python",
      "args": [
        "-m",
        "triagepilot",
        "--debugger-type",
        "auto",
        "--symbols-path",
        "/path/to/symbols",
        "--repo-path",
        "/path/to/repo",
        "--timeout",
        "60"
      ]
    }
  }
}
```

Config file locations:

- Cursor: `.cursor/mcp.json` or global MCP settings
- VS Code: `.vscode/mcp.json` (uses `"servers"` key)

## Usage

### One-Shot Triage (recommended)

```text
Analyze this crash dump:
- dump_path: /path/to/crash.dmp
- symbols_path: /path/to/symbols
- repo_path: /path/to/repo

Return crash summary, call stack, likely root cause, and fix suggestions.
```

### Session Mode

```text
1) open_dump
2) run_debugger_cmd (iterate as needed)
3) close_dump
```

### Natural Language Prompts

| You ask | TriagePilot does |
| --- | --- |
| "List crash dumps on this machine" | Runs `list_dumps` |
| "Analyze /tmp/core.1234" | Runs full analysis (`analyze_dump`) |
| "Show me thread list and modules" | Uses debugger commands + structured output |
| "Find the faulting source in my repo" | Attempts source localization |
| "Create PR with these fixes" | Uses `create_repo_pr` workflow |

### Prompt Template

Use built-in MCP prompt: `dump-triage`.

### Autonomous Triage (optional extra)

```bash
pip install -e ".[langgraph]"
export TRIAGEPILOT_LLM_API_KEY="sk-..."
```

Then call `auto_triage_dump` for debugger analysis + LLM reasoning + optional PR/patch generation.

## Available Tools

| Tool | Description |
| --- | --- |
| `analyze_dump` | One-shot crash dump analysis with stack/modules/threads/source lookup. |
| `open_dump` | Open dump and initialize analysis session. |
| `run_debugger_cmd` | Execute debugger command on active session. |
| `close_dump` | Close active dump session. |
| `list_dumps` | Discover dump files from platform-aware paths/patterns. |
| `create_repo_pr` | Create commit + branch + push + GitHub PR from local repo changes. |
| `create_shared_patch` | Generate markdown patch plan for shared/gitignored paths. |
| `auto_triage_dump` | Optional (`langgraph` extra): autonomous end-to-end triage flow. |

## CLI Options

```bash
triagepilot [OPTIONS]
# or
python -m triagepilot [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--debugger-type TYPE` | `auto` | Debugger backend: `auto`, `cdb`, `lldb`, `gdb` |
| `--debugger-path PATH` | Auto-detected | Full path to the debugger executable |
| `--cdb-path PATH` | Auto-detected | Path to `cdb.exe` (Windows, deprecated) |
| `--symbols-path PATH` | None | Path to symbol/debug info files |
| `--image-path PATH` | None | Path to executable/binary images |
| `--repo-path PATH` | None | Local repository path for source lookup |
| `--timeout SECONDS` | `30` | Default timeout for debugger commands |
| `--verbose` | Off | Enable debug-level logging |
| `--log-level LEVEL` | `INFO` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

## Environment Variables

Prefix: `TRIAGEPILOT_`

| Variable | Default | Description |
|---|---|---|
| `TRIAGEPILOT_DEBUGGER_TYPE` | `auto` | Debugger backend: `auto`, `cdb`, `lldb`, `gdb` |
| `TRIAGEPILOT_DEBUGGER_PATH` | Auto-detected | Path to the debugger executable |
| `TRIAGEPILOT_CDB_PATH` | Auto-detected | Path to `cdb.exe` (Windows, deprecated) |
| `TRIAGEPILOT_SYMBOLS_PATH` | None | Default symbols/debug info path |
| `TRIAGEPILOT_IMAGE_PATH` | None | Default executable image path |
| `TRIAGEPILOT_REPO_PATH` | None | Default repository path for source lookup |
| `TRIAGEPILOT_TIMEOUT` | `30` | Default command timeout (seconds) |
| `TRIAGEPILOT_VERBOSE` | `false` | Enable verbose logging |
| `TRIAGEPILOT_LOG_LEVEL` | `INFO` | Log level |
| `TRIAGEPILOT_MAX_CONCURRENT_SESSIONS` | `5` | Max simultaneous debugger sessions |
| `TRIAGEPILOT_LLM_PROVIDER` | `openai` | LLM provider (`openai`, `anthropic`, `azure`) |
| `TRIAGEPILOT_LLM_MODEL` | `gpt-4o` | LLM model name |
| `TRIAGEPILOT_LLM_API_KEY` | None | LLM API key (required for `auto_triage_dump`) |
| `TRIAGEPILOT_LANGSMITH_API_KEY` | None | LangSmith tracing API key |
| `TRIAGEPILOT_LANGSMITH_PROJECT` | `triagepilot` | LangSmith project name |
| `TRIAGEPILOT_MAX_RETRIES` | `3` | Max retries for LangGraph analysis nodes |

CLI args override env vars.

## Example Crash Programs

The `examples/` folder contains six C++ programs that intentionally crash. They work on all platforms.

| Example | Crash Type |
|---|---|
| `stack-overflow.cpp` | Unbounded recursion exhausts the stack |
| `use-after-free.cpp` | Dereference of freed heap memory |
| `double-free.cpp` | Free the same block twice |
| `vtable-corruption.cpp` | Virtual call on a deleted object |
| `stack-buffer-overrun.cpp` | Stack buffer overflow |
| `heap-corruption.cpp` | Write past heap allocation boundary |

### Build & Run

```bash
# Windows (MSVC) -- from Developer Command Prompt
cd examples && .\build.ps1

# Linux / macOS
cd examples && chmod +x build.sh && ./build.sh

# Run (Linux/macOS -- enable core dumps first)
ulimit -c unlimited
./build/out/stack-overflow
```

Then ask TriagePilot to analyze the resulting dump.

---

## Troubleshooting

### "Could not find cdb.exe" (Windows)

Install WinDbg from the [Microsoft Store](https://apps.microsoft.com/detail/9pgjgd53tn86) or the [Windows SDK](https://developer.microsoft.com/en-us/windows/downloads/windows-sdk/). If installed in a non-standard path:

```bash
triagepilot --debugger-path "D:\MyTools\cdb.exe"
```

### "Could not find gdb" (Linux)

```bash
sudo apt install gdb    # Debian/Ubuntu
sudo dnf install gdb    # Fedora/RHEL
```

### "Could not find lldb" (macOS)

```bash
xcode-select --install
```

### "Initialization timed out"

Increase timeout (often needed for first-time symbol downloads):

```bash
triagepilot --timeout 120
```

### No core dumps on Linux/macOS

```bash
ulimit -c unlimited
cat /proc/sys/kernel/core_pattern    # Linux
ls /cores/                           # macOS
```

On macOS, you may also need one-time system setup:

```bash
launchctl limit core unlimited unlimited
sudo mkdir -p /cores
sudo chmod 1777 /cores
sysctl kern.coredump kern.corefile
```

### Symbols not resolving on Windows

Set `_NT_SYMBOL_PATH` in MCP config:

```json
{
  "env": {
    "_NT_SYMBOL_PATH": "SRV*C:\\Symbols*https://msdl.microsoft.com/download/symbols"
  }
}
```

### `auto_triage_dump` missing

Install the extra and set LLM key:

```bash
pip install -e ".[langgraph]"
export TRIAGEPILOT_LLM_API_KEY="sk-..."
```

## Contributing

Contributions are welcome! Please feel free to open an issue or submit a pull request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes (`git commit -m 'Add my feature'`)
4. Push to the branch (`git push origin feature/my-feature`)
5. Open a Pull Request

### Running tests

```bash
pip install -e ".[langgraph]"
pip install pytest
pytest
```

## License

This project is licensed under the BSD 3-Clause License. See [LICENSE](LICENSE) for details.
