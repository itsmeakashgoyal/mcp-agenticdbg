# AGENTS.md

Instructions for AI assistants (Codex, Copilot, Claude, Cursor, etc.) working in this repository.

## Repository Overview

**TriagePilot** is an MCP server that bridges AI assistants to platform-native debuggers for crash dump analysis. Source code lives in `src/triagepilot/`.

## Development Setup

This project uses **[uv](https://docs.astral.sh/uv/)** for dependency management.

```bash
uv sync                          # Install core + dev deps (locked)
uv sync --extra langgraph        # Include optional LangGraph support
```

Fallback (pip):
```bash
pip install -e ".[langgraph]"
pip install ruff mypy pytest
```

## Running the Server

```bash
uv run triagepilot                                # Defaults (auto-detect platform debugger)
uv run triagepilot --debugger-type gdb --repo-path /path/to/repo
uv run python -m triagepilot                      # Alternative
```

## Testing

```bash
uv run pytest                    # All tests
uv run pytest -xvs               # Verbose, stop on first failure
uv run pytest tests/test_backends.py  # Single file
```

All tests must pass before committing. Tests mock debugger interactions and run on any OS.

## Linting & Formatting

```bash
uv run ruff check src/           # Lint (must pass)
uv run ruff format src/          # Format (must pass)
uv run mypy src/triagepilot/     # Type check
```

Configuration is in `pyproject.toml`: Python 3.10 target, 100-char line length, ruff rules `E/W/F/I/UP/B/SIM`.

## Source Layout

```
src/triagepilot/
  server.py           # MCP tool/prompt endpoints, Pydantic parameter models
  config.py           # pydantic-settings, all TRIAGEPILOT_* env vars
  backends/
    base.py           # Abstract DebuggerSession ABC
    cdb.py            # Windows CDB/WinDbg
    gdb.py            # Linux GDB (MI + CLI fallback)
    lldb.py           # macOS LLDB
  tools/
    debugger_tools.py # Session pooling, analysis, source localization
    git_tools.py      # PR/patch creation
  memory/             # SQLite-backed crash triage knowledge base
  graph/              # Optional LangGraph autonomous workflow
  prompts/            # MCP prompt templates
tests/                # pytest suite (mocked debugger interactions)
examples/             # Intentional crash programs (C++) for each platform
```

## Key Architecture Decisions

- **Backend abstraction**: `DebuggerSession` ABC in `backends/base.py`. Platform detection in `backends/__init__.py`. Each backend spawns a subprocess, communicates over stdin/stdout.
- **Session pooling**: `debugger_tools.py` manages concurrent sessions with LRU eviction. Sessions keyed by dump path.
- **Source localization**: Multi-level fallback: debug info -> symbol search -> stack trace search across the entire repo tree (including gitignored dirs).
- **Command security**: Blocklist in `debugger_tools.py` prevents dangerous commands (`.shell`, `.kill`, `.dump`, etc.). Rate-limited at 10 ops/sec.
- **LangGraph**: Optional (`try/import` guarded). Enables `auto_triage_dump` multi-step autonomous workflow.
- **Memory system**: SQLite at `~/.triagepilot/memory.db`. Auto-save on analysis, auto-recall of similar past crashes. Three-tier similarity: signature (50%), stack hash (30%), TF-IDF (20%).

## MCP Tools

| Tool | Description |
|------|-------------|
| `analyze_dump` | Full crash analysis + source localization |
| `open_dump` | Open a dump file and return initial analysis |
| `run_debugger_cmd` | Execute a validated debugger command |
| `send_ctrl_break` | Interrupt a running debugger command |
| `close_dump` | Close a session |
| `list_dumps` | Discover dump files on the system |
| `create_repo_pr` | Create GitHub PR with fix |
| `create_shared_patch` | Create markdown patch for gitignored components |
| `auto_triage_dump` | Autonomous triage via LangGraph (optional) |
| `recall_similar_crashes` | Search memory for similar past crash analyses |
| `save_triage_result` | Save root cause and fix to memory |
| `list_known_patterns` | Browse stored crash patterns |
| `forget_pattern` | Delete a memory entry by ID |

## Crash Dump Analysis Workflow

When asked to analyze a crash dump:

1. **Identify the dump**: Use `list_dumps` if no path given, or accept path from user.
2. **Run analysis**: Call `analyze_dump` with `dump_path`, optional `symbols_path`, `image_path`, `repo_path`.
3. **Deep inspection**: Use `run_debugger_cmd` for crash-type-specific commands (see `src/triagepilot/prompts/dump-triage.prompt.md` for the full playbook).
4. **Report**: Produce structured markdown with exception details, stack trace, root cause, and fix.
5. **Fix**: Apply the fix in the source. Use `create_repo_pr` for repo-tracked files, `create_shared_patch` for gitignored/vendor files.

## CI/CD

GitHub Actions workflows in `.github/workflows/`:
- **ci.yml**: Lint (ruff), type check (mypy), and test (pytest) across Python 3.10-3.12 on Ubuntu/macOS/Windows. Uses `uv` for dependency management.
- **dco.yml**: DCO sign-off check on PRs.

## Commits & PRs

- All commits require DCO sign-off (`git commit -s`)
- Run `uv run ruff check src/ && uv run ruff format --check src/ && uv run pytest` before committing
- Branch naming: `users/<ldap>/<feature>` for PRs
