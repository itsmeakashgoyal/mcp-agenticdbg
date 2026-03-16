# Troubleshooting

## Debugger Not Found

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

## Timeout Issues

### "Initialization timed out"

Increase timeout (often needed for first-time symbol downloads):

```bash
triagepilot --timeout 120
```

## Core Dump Issues

### No core dumps on Linux

```bash
ulimit -c unlimited
cat /proc/sys/kernel/core_pattern
```

If `core_pattern` points to a crash reporter (e.g. Ubuntu's `apport`), cores go to
`/var/crash/`. Disable apport or adjust the pattern:

```bash
echo "core" | sudo tee /proc/sys/kernel/core_pattern
```

### No core dumps on macOS

On macOS 12+, `ulimit -c unlimited` is not enough — the system's `ReportCrash` agent
intercepts fatal signals before the kernel writes to `/cores`. Crashes land in
`~/Library/Logs/DiagnosticReports/` as `.crash` / `.ips` reports, not binary core files.

Use the included `gen_core_mac.sh` script, which runs the binary under `lldb` and saves
the core with `process save-core`:

```bash
cd examples
./gen_core_mac.sh use-after-free       # writes build/out/core.use-after-free
./gen_core_mac.sh stack-overflow /tmp/stack.core   # custom output path
```

This approach works on all macOS versions without requiring `/cores` to be writable or
disabling SIP.

## Symbol Resolution

### Symbols not resolving on Windows

Set `_NT_SYMBOL_PATH` in MCP config:

```json
{
  "env": {
    "_NT_SYMBOL_PATH": "SRV*C:\\Symbols*https://msdl.microsoft.com/download/symbols"
  }
}
```

## LangGraph / Autonomous Triage

### `auto_triage_dump` missing

Install the extra and set LLM key:

```bash
pip install -e ".[langgraph]"
export TRIAGEPILOT_LLM_API_KEY="sk-..."
```
