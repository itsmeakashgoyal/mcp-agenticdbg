# Contributing to TriagePilot

Thanks for your interest in contributing! This guide will help you get started.

## Getting Started

```bash
git clone https://github.com/AkashGoyal/mcp-agenticdbg.git
cd mcp-agenticdbg
pip install -e ".[langgraph]"
```

## Development Setup

Install dev dependencies:

```bash
pip install ruff mypy pytest
```

Or using dependency groups:

```bash
pip install --dependency-groups dev -e ".[langgraph]"
```

## Code Style

We use [ruff](https://docs.astral.sh/ruff/) for linting and formatting. Run before committing:

```bash
ruff check src/           # Lint
ruff check --fix src/     # Lint with auto-fix
ruff format src/          # Format
```

Configuration is in `pyproject.toml`. Key settings: Python 3.10 target, 100 char line length.

## Type Checking

```bash
mypy src/triagepilot/
```

We use `ignore_missing_imports` since some dependencies lack type stubs.

## Running Tests

```bash
pytest                    # Run all tests
pytest -xvs              # Verbose, stop on first failure
pytest src/triagepilot/tests/test_backends.py  # Single file
```

Most tests mock platform-specific debugger interactions and run on any OS. Integration tests that require real debuggers should be run on the appropriate platform.

## Project Structure

See `CLAUDE.md` for detailed architecture documentation. Key directories:

- `src/triagepilot/backends/` — Debugger adapters (CDB, GDB, LLDB)
- `src/triagepilot/tools/` — MCP tool implementations
- `src/triagepilot/graph/` — Optional LangGraph triage workflow
- `src/triagepilot/tests/` — Test suite

## Filing Issues

We use GitHub issue templates for all bug reports and feature requests. When you open a new issue, you'll be prompted to choose a template:

- **Bug Report** — for crashes, incorrect behavior, or regressions
- **Feature Request** — for new functionality or improvements

Blank issues are disabled. If your issue doesn't fit a template, start a [discussion](https://github.com/AkashGoyal/mcp-agenticdbg/discussions) instead.

## DCO (Developer Certificate of Origin)

All contributions require a DCO sign-off. This is a lightweight way to certify that you wrote or have the right to submit the code. See [developercertificate.org](https://developercertificate.org/) for the full text.

**How to sign off:**

Add `-s` when committing:

```bash
git commit -s -m "Fix GDB output parsing for async frames"
```

This appends a `Signed-off-by: Your Name <your@email.com>` trailer using your git config.

**Retroactively sign off** (if you forgot):

```bash
git rebase --signoff HEAD~N   # where N is the number of commits to fix
git push --force-with-lease
```

The DCO check runs automatically on all PRs and must pass before merging.

## Pull Request Process

1. Fork the repo and create a branch from `master`
2. Make your changes
3. Ensure all checks pass locally:
   ```bash
   ruff check src/
   ruff format --check src/
   mypy src/triagepilot/
   pytest
   ```
4. Fill in the PR template
5. Submit your PR — CI will run lint, type-check, and tests automatically

All CI checks must pass before a PR can be merged.

## Platform Notes

TriagePilot supports three debugger backends:

| Backend | Platform | Debugger |
|---------|----------|----------|
| CDB | Windows | CDB/WinDbg |
| GDB | Linux | GDB |
| LLDB | macOS | LLDB |

Unit tests with mocked debugger interactions work cross-platform. If you're adding or modifying backend-specific code, please test on the relevant platform when possible.

## License

This project is licensed under the BSD 3-Clause License. By contributing, you agree that your contributions will be licensed under the same terms.
