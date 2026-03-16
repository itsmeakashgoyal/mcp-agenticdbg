# macOS -- Build, Crash, and Analyze

Complete walkthrough for analyzing crash dumps on macOS using LLDB.

## 1. Prerequisites

- **Xcode Command Line Tools** (provides `clang++` and `lldb`)

```bash
xcode-select --install

# Verify
clang++ --version
lldb --version
```

## 2. Build

```bash
cd examples/macos
chmod +x build.sh
./build.sh
```

This compiles all 10 examples from `../common/` with full debug symbols (`-g -O0 -std=c++17`).

Output: `build/out/<name>` (executables with embedded DWARF) + `build/out/<name>.dSYM/` (debug symbol bundles)

## 3. Generate Core Dumps

Use `gen_core_mac.sh` -- it runs the binary under `lldb` and saves the core automatically:

```bash
chmod +x gen_core_mac.sh

# Generate a core dump (saved to build/out/core.<name>)
./gen_core_mac.sh use-after-free

# Or specify a custom output path
./gen_core_mac.sh stack-overflow /tmp/stack-overflow.core
```

The script prints the core path on success:

```
Core dump written to: build/out/core.use-after-free
```

> **Why not just run the binary directly?**
> On macOS 12+, the system's `ReportCrash` agent intercepts fatal signals before the kernel writes to `/cores`. You get a `.crash` text report in `~/Library/Logs/DiagnosticReports/` instead of a binary core dump. `gen_core_mac.sh` works around this by using `lldb --one-line-on-crash "process save-core"` to capture the core at the exact crash point.

## 4. Analyze with TriagePilot (MCP Tools)

Start TriagePilot and use the MCP tools from your AI assistant:

```
# Full crash analysis (runs bt all, registers, frame info, modules)
analyze_dump  examples/macos/build/out/core.use-after-free

# Run specific LLDB commands
run_debugger_cmd  bt
run_debugger_cmd  bt all
run_debugger_cmd  thread list
run_debugger_cmd  register read
run_debugger_cmd  frame variable
run_debugger_cmd  disassemble --pc --count 30

# Close the session when done
close_dump  examples/macos/build/out/core.use-after-free
```

## 5. Analyze with Python Demo

Run the standalone LLDB triage demo directly:

```bash
python lldb_triage_demo.py build/out/core.use-after-free \
    --image build/out/use-after-free
```

This calls every inspection method (crash summary, all-thread backtraces, registers, locals, disassembly, loaded images) and prints a structured report.

## 6. Useful LLDB Commands

Quick reference for interactive debugging via `run_debugger_cmd`:

| Command | What it does |
|---------|-------------|
| `bt` | Stack trace of current thread |
| `bt all` | Stack traces of **all** threads |
| `thread list` | List all threads |
| `thread info` | Info about current (crashing) thread |
| `register read` | Display registers |
| `register read --all` | All register sets (general + floating-point) |
| `frame select N` | Select stack frame N |
| `frame variable` | Show local variables in current frame |
| `expression expr` | Evaluate expression |
| `disassemble --pc --count 30` | Disassemble 30 instructions at crash point |
| `disassemble --name func` | Disassemble named function |
| `memory read --count 64 --format x <addr>` | Hex dump 64 bytes at address |
| `process status` | Crash signal and description |
| `image list` | List loaded images/frameworks |
| `target list` | Target binary info |

## 7. Try These Examples

**Start simple:**
```bash
./gen_core_mac.sh stack-overflow
# Then: analyze_dump build/out/core.stack-overflow
```

**Try a complex one:**
```bash
./gen_core_mac.sh multi-inheritance-crash
# Wrong C-style cast across multiple inheritance
# Can the analysis identify the vtable corruption?
```

**Multi-threaded crash:**
```bash
./gen_core_mac.sh thread-uaf
# Two threads race on the same object
# Check "bt all" to see what both threads were doing
```

## Troubleshooting

**Core dump not created:**
```bash
# 1. Make sure the binary exists
ls build/out/use-after-free

# 2. Try running lldb manually
lldb build/out/use-after-free -o run

# 3. Check disk space
df -h .
```

**LLDB not found:**
```bash
# Reinstall Xcode Command Line Tools
sudo rm -rf /Library/Developer/CommandLineTools
xcode-select --install
```
