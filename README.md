# TriagePilot

**AI-powered, cross-platform crash dump triage for developers.**

TriagePilot is an [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server that brings intelligent crash dump analysis directly into your AI-powered IDE. Connect it to **Cursor**, **VS Code**, or any MCP-compatible client, and ask questions about crash dumps in plain English. The AI assistant drives the debugger, locates faulting source code in your repo, performs root cause analysis, and can even suggest fixes -- all without you ever opening a debugger manually.

**Supported platforms:**

| Platform | Debugger | Dump Types |
|----------|----------|------------|
| Windows  | CDB / WinDbg | `.dmp` (MiniDump / full dump) |
| Linux    | GDB | Core dumps (`core.*`) |
| macOS    | LLDB | Core dumps, `.crash` reports |

Works with binaries compiled by **MSVC**, **Clang**, **GCC**, or any compiler that produces standard debug information (PDB, DWARF).

---

## Why TriagePilot?

- **Zero debugger expertise required.** Ask "What caused this crash?" and get an answer, not a register dump.
- **Cross-platform.** One tool for Windows, Linux, and macOS crashes. The backend auto-detects your platform.
- **Source-aware.** Point it at your repo and it finds the faulting function, even when debug symbols only have public names.
- **AI-native.** Built as an MCP server so AI assistants can orchestrate multi-step triage autonomously.
- **Secure.** Dangerous debugger commands are blocklisted. Rate limiting prevents runaway tool calls.
- **Extensible.** Optional LangGraph integration enables fully autonomous end-to-end triage with LLM-powered root cause analysis and fix suggestions.

---

## Table of Contents

- [Quick Start](#quick-start)
- [How It Works](#how-it-works)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Available Tools](#available-tools)
- [CLI Options](#cli-options)
- [Environment Variables](#environment-variables)
- [Example Crash Programs](#example-crash-programs)
- [Project Structure](#project-structure)
- [Troubleshooting](#troubleshooting)

---

## Quick Start

```bash
# 1. Install
pip install -e .

# 2. Add to your MCP config (Cursor / VS Code)
# See Configuration section below

# 3. Ask your AI assistant
"Analyze the crash dump at /path/to/crash.dmp"
```

---

## How It Works

```
You (in Cursor / VS Code)
  |
  |  "What caused this crash?"
  v
AI Assistant  --->  TriagePilot MCP Server  --->  Debugger Backend
                                                     |
                        +----------------------------+----------------------------+
                        |                            |                            |
                   CDB (Windows)               GDB (Linux)                LLDB (macOS)
                        |                            |                            |
                   .dmp files                  core dumps                 .crash / core dumps
```

TriagePilot acts as a bridge between your AI assistant and the platform-native debugger. When you ask the AI to analyze a crash:

1. **The AI calls TriagePilot's MCP tools** (`analyze_dump`, `run_debugger_cmd`, etc.)
2. **TriagePilot auto-detects your platform** and launches the right debugger (CDB on Windows, GDB on Linux, LLDB on macOS)
3. **The debugger analyzes the crash dump** and returns structured results (stack traces, modules, crash info)
4. **TriagePilot locates the faulting source** in your local repo using a multi-level search (debug info -> symbol name -> stack trace)
5. **The AI interprets the results** and explains the root cause, suggests fixes, and can create a PR

---

## Prerequisites

### Python 3.10 or later

```bash
python --version
```

### Debugger (one per platform)

#### Windows: CDB / WinDbg

Install via one of:
- **Microsoft Store:** Search for [WinDbg](https://apps.microsoft.com/detail/9pgjgd53tn86) and click Install.
- **Windows SDK:** Download the [Windows SDK](https://developer.microsoft.com/en-us/windows/downloads/windows-sdk/) and select "Debugging Tools for Windows".

CDB also works with **Clang-compiled** Windows binaries when Clang produces PDB debug info.

#### Linux: GDB

```bash
# Debian / Ubuntu
sudo apt install gdb

# Fedora / RHEL
sudo dnf install gdb

# Arch
sudo pacman -S gdb
```

Enable core dumps: `ulimit -c unlimited`

#### macOS: LLDB

```bash
xcode-select --install
```

### Cursor or VS Code

- [Cursor](https://www.cursor.com/) (recommended)
- [VS Code](https://code.visualstudio.com/) with MCP support

---

## Installation

### From source

```bash
git clone https://github.com/AkashGoyal2003/win_crashdbg.git
cd win_crashdbg

# (Optional) Create a virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

# Install
pip install -e .

# Verify
triagepilot --help
```

### With LangGraph support (autonomous triage)

```bash
pip install -e ".[langgraph]"
```

This adds `langgraph`, `langchain-core`, and `langchain-openai` for the `auto_triage_dump` tool.

### Migrating from win-crashdbg

If you previously had the old `win-crashdbg` package installed:

```bash
pip uninstall win-crashdbg -y
pip install -e .
```

Update your MCP config to use the new name (see [Configuration](#configuration)).

---

## Configuration

### Minimal MCP config

The server auto-detects the platform and debugger. This works for all platforms:

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

### Windows (with Microsoft symbol server)

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

### With custom options

```json
{
  "mcpServers": {
    "triagepilot": {
      "type": "stdio",
      "command": "python",
      "args": [
        "-m", "triagepilot",
        "--symbols-path", "/path/to/symbols",
        "--repo-path", "/path/to/your/repo",
        "--timeout", "60"
      ]
    }
  }
}
```

### Where to put the config

- **Cursor:** Settings (Ctrl+Shift+J) -> MCP -> "+ Add new global MCP server", or `.cursor/mcp.json` in your project root.
- **VS Code:** `.vscode/mcp.json` in your project root (use `"servers"` instead of `"mcpServers"`).

---

## Usage

### One-shot analysis (recommended)

Use `analyze_dump` for most cases. It runs a full analysis in one call:

```text
Use TriagePilot to analyze this crash:
- dump_path: /path/to/crash.dmp
- symbols_path: /path/to/symbols
- repo_path: /code/myapp

What caused the crash? Suggest a fix.
```

### Full triage workflow

For a complete triage with PR creation:

```text
Use TriagePilot to analyze:
- dump_path: C:\Users\me\Downloads\case-42\crash.dmp
- symbols_path: C:\Users\me\Downloads\case-42\symbols
- image_path: C:\Users\me\Downloads\case-42\bin\MyApp.exe
- repo_path: C:\code\myapp
- jira_id: APP-12345

Return:
1) exception code + faulting module/function
2) full symbolized stack trace
3) likely root cause
4) concrete fixes in current repo and apply them
5) verification steps
6) create a PR once changes are finalized
```

### Guided prompt

Use the built-in `/dump-triage` prompt in Cursor for a structured, step-by-step workflow.

### Natural language

Once configured, just ask naturally:

| What you can say | What happens |
|---|---|
| "List crash dumps on my system" | Finds dump files in the default directory |
| "Analyze the crash dump at /path/to/crash.dmp" | Full crash analysis with stack traces |
| "What caused this crash?" | Root cause analysis |
| "Show me the call stack" | Runs the debugger's stack trace command |
| "Run `!analyze -v` on the current dump" | Executes a specific debugger command |
| "Close the dump" | Releases debugger resources |

### Autonomous triage (LangGraph)

With the `langgraph` extra and an LLM API key:

```bash
pip install -e ".[langgraph]"
export TRIAGEPILOT_LLM_API_KEY="sk-..."
```

Then use `auto_triage_dump` for fully autonomous end-to-end analysis: debugger analysis -> metadata extraction -> source lookup -> LLM root cause analysis -> fix suggestions -> PR/patch creation.

---

## Available Tools

| Tool | Description |
|---|---|
| `analyze_dump` | One-shot crash dump analysis. Runs platform-appropriate analysis and returns crash info, stack trace, faulting source, modules, and threads. |
| `list_dumps` | List crash dump files in a directory. Auto-detects platform-appropriate file types. |
| `open_dump` | Open a crash dump and run initial analysis. |
| `run_debugger_cmd` | Execute any debugger command. Includes security blocklist and rate limiting. |
| `close_dump` | Close a dump session and free resources. |
| `create_repo_pr` | Create commit/push/PR from local repo changes. |
| `create_shared_patch` | Create a markdown patch for shared/gitignored changes. |
| `auto_triage_dump` | *(langgraph extra)* Autonomous end-to-end triage with LLM analysis. |

---

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

All settings use the `TRIAGEPILOT_` prefix:

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

CLI arguments override environment variables.

---

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

## Project Structure

```
triagepilot/
├── src/
│   └── triagepilot/
│       ├── __init__.py             # Entry point, CLI
│       ├── __main__.py             # python -m triagepilot
│       ├── server.py               # MCP server: tools, prompts
│       ├── config.py               # ServerConfig (pydantic-settings)
│       ├── logging_config.py       # Structured logging (structlog)
│       ├── backends/               # Debugger backends
│       │   ├── __init__.py         # Factory, platform detection
│       │   ├── base.py             # DebuggerSession ABC
│       │   ├── cdb.py              # CDB/WinDbg (Windows)
│       │   ├── lldb.py             # LLDB (macOS/Linux)
│       │   └── gdb.py              # GDB (Linux)
│       ├── tools/
│       │   ├── debugger_tools.py   # Platform-agnostic tool handlers
│       │   └── git_tools.py        # PR/patch handlers
│       ├── graph/                  # LangGraph (optional)
│       │   ├── state.py            # State schema
│       │   ├── nodes.py            # Node functions
│       │   ├── edges.py            # Edge logic
│       │   └── graph.py            # Graph builder
│       ├── prompts/
│       │   └── dump-triage.prompt.md
│       └── tests/
│           ├── test_backends.py
│           ├── test_cdb_session.py
│           ├── test_helpers.py
│           ├── test_git_tools.py
│           └── test_config.py
├── examples/                       # Cross-platform crash programs
│   ├── build.ps1 / build.sh
│   ├── crashdump.h
│   └── *.cpp
├── pyproject.toml
└── README.md
```

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

### "Command blocked for security reasons"

Intentional -- dangerous commands are blocklisted. Run the debugger directly if you need a blocked command.

### No core dumps generated (Linux / macOS)

```bash
ulimit -c unlimited
cat /proc/sys/kernel/core_pattern    # Linux
ls /cores/                           # macOS
```

### Symbols not resolving (Windows)

Set `_NT_SYMBOL_PATH` in your MCP config:

```json
"env": {
  "_NT_SYMBOL_PATH": "SRV*C:\\Symbols*https://msdl.microsoft.com/download/symbols"
}
```

### MCP server not appearing in IDE

1. Check config file location (`.cursor/mcp.json` or `.vscode/mcp.json`).
2. Restart your IDE.
3. Verify `python` is on your PATH, or use the full path to the Python executable.

### auto_triage_dump tool not showing

```bash
pip install -e ".[langgraph]"
```

Set `TRIAGEPILOT_LLM_API_KEY` for LLM nodes.
