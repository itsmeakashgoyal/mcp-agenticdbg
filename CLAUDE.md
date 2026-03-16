# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**TriagePilot** is an MCP (Model Context Protocol) server that bridges AI assistants to platform-native debuggers (CDB/WinDbg on Windows, GDB on Linux, LLDB on macOS) for runtime-grounded crash dump analysis. It exposes debugger capabilities as MCP tools so AI assistants can perform structured crash triage with real debugger output.

## Commands

### Installation
```bash
pip install -e .                          # Install in editable mode (core deps only)
pip install -e ".[langgraph]"            # Install with optional LangGraph support
```

### Running the MCP Server
```bash
triagepilot                               # Run with defaults
triagepilot --debugger gdb --repo-path /path/to/repo
python -m triagepilot                     # Alternative invocation
```

### Running Tests
```bash
pytest                                    # Run all tests
pytest -xvs                              # Verbose, stop on first failure
PYTHONPATH=src pytest -q                 # Quiet output
pytest tests/test_backends.py                 # Single test file
```

### Building Example Crash Programs
```bash
cd examples/linux && bash build.sh       # Linux
cd examples/macos && bash build.sh       # macOS
cd examples\windows && .\build.ps1       # Windows (MSVC)
```

## Architecture

### Layer Overview

```
MCP Protocol (stdio)
    └── server.py             # Tool/prompt endpoints, Pydantic parameter models
         ├── tools/           # Tool implementations
         │    ├── debugger_tools.py   # Session pooling, crash analysis, source localization
         │    └── git_tools.py        # Git workflows, PR/patch creation
         ├── backends/        # Platform-specific debugger adapters
         │    ├── base.py     # Abstract DebuggerSession interface
         │    ├── cdb.py      # Windows CDB/WinDbg
         │    ├── gdb.py      # Linux GDB (dual-mode: MI + CLI fallback)
         │    └── lldb.py     # macOS LLDB
         ├── memory/          # Persistent crash triage knowledge base
         │    ├── models.py   # Pydantic models (TriageMemoryEntry, tool params)
         │    ├── store.py    # SQLite-backed MemoryStore (CRUD, decay, pruning)
         │    ├── similarity.py # TF-IDF scoring, multi-tier crash matching
         │    ├── signature.py  # Crash signature extraction, stack hashing
         │    └── tools.py    # MCP tool handlers + auto-save/recall helpers
         ├── config.py        # pydantic-settings, TRIAGEPILOT_* env vars
         └── graph/           # Optional LangGraph autonomous triage workflow
```

### Key Design Decisions

**Debugger Backend Abstraction:** `backends/base.py` defines a `DebuggerSession` ABC. Platform detection happens in `backends/__init__.py` via `detect_debugger_type()`. Each backend spawns a subprocess and communicates over stdin/stdout.

**GDB Dual-Mode:** `backends/gdb.py` defaults to MI (Machine Interface) mode for structured output, falling back to CLI text mode. Uses a dedicated output-reading thread with an activity-based timeout and "quiet drain" logic to handle async output ordering issues.

**Session Pooling:** `tools/debugger_tools.py` manages concurrent sessions with configurable limits. Sessions are keyed by dump path.

**Source Localization:** `handle_analyze_dump` runs multi-level fallback to find the faulting source file: debug info → symbol search → stack trace search across the repo.

**LangGraph Integration:** The `graph/` package is optional (guarded by try/import). When available, it enables `auto_triage_dump` — a multi-step autonomous workflow with state schema in `graph/state.py`.

### MCP Tools Exposed

| Tool | Description |
|------|-------------|
| `analyze_dump` | Full crash analysis + source localization |
| `open_dump` | Open a dump file and return session ID |
| `run_debugger_cmd` | Run arbitrary (validated) debugger command |
| `close_dump` | Close a session |
| `list_dumps` | Discover dump files on the system |
| `create_repo_pr` | Create GitHub PR with fix |
| `create_shared_patch` | Create markdown patch for gitignored components |
| `auto_triage_dump` | Autonomous triage via LangGraph (optional) |
| `recall_similar_crashes` | Search memory for similar past crash analyses |
| `save_triage_result` | Save root cause and fix to memory |
| `list_known_patterns` | Browse stored crash patterns |
| `forget_pattern` | Delete a memory entry by ID |

### Persistent Memory System

The `memory/` package provides a SQLite-backed knowledge base that learns from crash triage sessions. Key features:

- **Auto-save:** Every `analyze_dump` call automatically stores the crash signature, stack hash, and analysis tokens
- **Auto-recall:** On new analyses, similar past crashes are surfaced before the main results
- **Three-tier similarity:** Crash signature match (50%), stack hash match (30%), TF-IDF keyword similarity (20%)
- **Confidence decay:** Old memories decay with a configurable half-life (default 90 days); confirmed fixes boost confidence
- **Auto-tagging:** Crash type, module, language, and category tags are extracted automatically
- **LangGraph integration:** `memory_recall_node` feeds past cases into LLM root cause analysis; `memory_save_node` persists completed triages

Data stored at `~/.triagepilot/memory.db` by default.

### Configuration

All settings configurable via environment variables (`TRIAGEPILOT_` prefix) or CLI flags. Key settings in `config.py`:
- `debugger_type`: auto-detected or explicit (cdb/gdb/lldb)
- `debugger_path`: path to debugger binary
- `symbols_path`, `image_path`, `repo_path`
- `session_timeout`, `max_sessions`
- `llm_model`, `llm_api_key` (for LangGraph mode)
- `memory_enabled`, `memory_db_path`, `memory_max_entries`, `memory_confidence_half_life_days`
- `memory_auto_recall`, `memory_auto_save`

### Prompt Template

`src/triagepilot/prompts/dump-triage.prompt.md` contains the comprehensive triage workflow prompt served as an MCP prompt endpoint. It defines the step-by-step workflow for AI assistants using this server.
