# GDB Enhanced Dump Triage - Quick Start

## What Was Fixed

Your GDB and LLDB backends had a critical "output lag" bug where command N's output would appear on command N+1. This is now **completely fixed** with proper output delimiting and quiet-drain logic.

Additionally, GDB now has **dual-mode support**:
- **MI mode** (default): structured, machine-readable output
- **CLI mode**: traditional text mode with robust parsing

## Quick Test (Verify It Works)

```bash
cd /Users/akashgoyal/Documents/personal_projects/mcp-agenticdbg

# Run all tests (should see 99 passed)
PYTHONPATH=src pytest -q

# Test with a real core dump
PYTHONPATH=src python3 -c "
from triagepilot.backends.gdb import GDBSession

# Replace with your actual core dump path
dump = '/var/crash/core.12345'
# dump = '/path/to/your/core'

session = GDBSession(dump, use_mi=True, timeout=30)
summary = session.get_crash_summary()

print('Signal:', summary.get('signal'))
print('Threads:', len(summary.get('threads', [])))
print('Registers:', len(summary.get('registers', {})))
print('Backtrace frames:', len(summary.get('backtrace', [])))
"
```

## Using via MCP Server

The MCP server automatically benefits from these improvements:

```bash
# Start the server (in one terminal)
cd /Users/akashgoyal/Documents/personal_projects/mcp-agenticdbg
python -m triagepilot.server

# In Cursor/VSCode, the server is already configured
# Just call the tools:
#   - analyze_dump
#   - open_dump
#   - run_debugger_cmd
```

## Direct Python Usage

### Basic Dump Analysis
```python
from triagepilot.backends import create_session

# Auto-detect backend (GDB on Linux, LLDB on macOS, CDB on Windows)
session = create_session(
    dump_path="/var/crash/core.12345",
    symbols_path="/usr/lib/debug",  # Optional
)

# Get crash information
crash_info = session.get_crash_info()
stack = session.get_stack_trace()
modules = session.get_loaded_modules()
threads = session.get_threads()
```

### Advanced GDB (MI Mode)
```python
from triagepilot.backends.gdb import GDBSession

# Explicitly use GDB with MI mode
session = GDBSession(
    dump_path="/var/crash/core.12345",
    use_mi=True,  # Enable machine interface
    symbols_path="/usr/lib/debug:/opt/app/symbols",
    timeout=30,
)

# Get structured crash summary (no text parsing!)
summary = session.get_crash_summary()

print(f"Crash Signal: {summary['signal']}")
print(f"Thread Count: {len(summary['threads'])}")

# Structured backtrace
for i, frame in enumerate(summary['backtrace'][:10]):
    func = frame.get('func', '??')
    file = frame.get('file', '??')
    line = frame.get('line', '??')
    print(f"  #{i} {func} at {file}:{line}")

# Get specific variables
errno = session.get_variable("errno")
ptr = session.get_variable("my_pointer")

# Execute console commands through MI
output = session.send_command("info registers")

# Or send pure MI commands
result = session.send_mi_command("data-list-register-values x")
registers = result.get('results', {}).get('register-values', [])
```

### Legacy CLI Mode (Backward Compatible)
```python
from triagepilot.backends.gdb import GDBSession

# Use traditional CLI mode
session = GDBSession(
    dump_path="/var/crash/core.12345",
    use_mi=False,  # Disable MI, use CLI
)

# Same API, but returns text lines instead of structured data
output = session.send_command("bt full")
for line in output:
    print(line)
```

## MCP Tool Usage in Cursor

Once your MCP server is running, you can use it in Cursor:

1. **Analyze a dump:**
   ```
   Use the analyze_dump tool with dump_path="/var/crash/core.12345"
   ```

2. **Interactive debugging:**
   ```
   1. Use open_dump with dump_path="/var/crash/core.12345"
   2. Use run_debugger_cmd with command="info locals"
   3. Use run_debugger_cmd with command="p errno"
   ```

3. **List available dumps:**
   ```
   Use list_dumps to see all crash dumps in /var/crash
   ```

## Performance Tips

1. **Use MI mode for triage:** More reliable parsing, especially for registers/frames
2. **Use CLI mode for complex commands:** Some advanced GDB features work better in CLI
3. **Set appropriate timeouts:** 
   - Fast commands: 10s (default)
   - Slow commands (e.g., `info sharedlibrary`): 60s+ (automatic activity timeout)

## Troubleshooting

### "GDB process timed out"
- Increase timeout: `GDBSession(dump_path, timeout=60)`
- Check if symbols are loading (this can be slow)
- Verify GDB is installed: `which gdb`

### "No structured data in MI mode"
- Fall back to CLI: `use_mi=False`
- Check GDB version supports MI2: `gdb --version`
- Some commands only work in console mode

### "Command output is empty"
- This was the old bug, should be fixed now
- If you still see it, file an issue with reproducible example

## What's Different from ChatDBG/AgentGDB

| Feature | ChatDBG/AgentGDB | mcp-agenticdbg |
|---------|------------------|----------------|
| Architecture | GDB plugin (runs inside GDB) | External controller (MCP server) |
| Integration | `.gdbinit` script loading | VSCode/Cursor MCP tool |
| Commands | `why`, `chat` in GDB prompt | `analyze_dump`, `run_debugger_cmd` in editor |
| Best for | Interactive GDB sessions | Automated dump triage, editor workflows |

## Next Steps

1. ✅ Test with your real core dumps
2. ✅ Try both MI and CLI modes to see which works better for your use case
3. ✅ Integrate with your MCP workflows in Cursor
4. Consider contributing improvements back (PRs welcome!)

## Files Created

- `src/triagepilot/backends/gdb.py` - Enhanced GDB backend (650 lines)
- `src/triagepilot/backends/lldb.py` - Fixed LLDB backend (267 lines)
- `examples/gdb_triage_demo.py` - Demonstration script
- `IMPROVEMENTS.md` - Detailed technical documentation
- `QUICKSTART_GDB.md` - This file

## Support

If you encounter issues:
1. Check `IMPROVEMENTS.md` for technical details
2. Run the demo: `python examples/gdb_triage_demo.py`
3. Review test output: `pytest -xvs`
4. Check logs: set `verbose=True` in session constructor
