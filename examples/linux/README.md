# Linux -- Build, Crash, and Analyze

Complete walkthrough for analyzing crash dumps on Linux using GDB.

## 1. Prerequisites

- **GCC** (`g++`) or **Clang** (`clang++`)
- **GDB** (for analysis)

```bash
# Debian/Ubuntu
sudo apt install g++ gdb

# Fedora/RHEL
sudo dnf install gcc-c++ gdb

# Verify
g++ --version
gdb --version
```

## 2. Build

```bash
cd examples/linux
chmod +x build.sh
./build.sh
```

This compiles all 10 examples from `../common/` with full debug symbols (`-g -O0 -std=c++17`).

Output: `build/out/<name>` (executables with embedded DWARF debug info)

Override compiler if needed:

```bash
CXX=clang++ ./build.sh
```

## 3. Generate Core Dumps

Enable core dumps and run an example:

```bash
ulimit -c unlimited
./build/out/use-after-free
```

The program crashes and the OS writes a core dump. Where it goes depends on your system:

```bash
# Check your core pattern
cat /proc/sys/kernel/core_pattern
```

| Pattern | Core location |
|---------|--------------|
| `core` | Current directory as `core` or `core.<pid>` |
| `core.%e.%p` | Current directory as `core.<name>.<pid>` |
| `\|/usr/share/apport/apport ...` | Ubuntu Apport: `/var/crash/` |
| `/tmp/cores/core.%e.%p` | Custom path |

**If cores aren't appearing**, set the pattern explicitly:

```bash
# Write cores to current directory (requires root)
sudo sysctl -w kernel.core_pattern=core.%e.%p

# Then re-run
./build/out/use-after-free
ls core.*
```

## 4. Analyze with TriagePilot (MCP Tools)

Start TriagePilot and use the MCP tools from your AI assistant:

```
# Full crash analysis (runs bt full, thread info, registers, modules)
analyze_dump  examples/linux/core.use-after-free.1234

# Run specific GDB commands
run_debugger_cmd  bt full
run_debugger_cmd  info threads
run_debugger_cmd  info registers
run_debugger_cmd  info locals
run_debugger_cmd  disassemble

# Close the session when done
close_dump  examples/linux/core.use-after-free.1234
```

## 5. Analyze with Python Demo

Run the standalone GDB triage demo directly:

```bash
python gdb_triage_demo.py core.use-after-free.1234 \
    --image build/out/use-after-free
```

This calls every inspection method (crash summary, all-thread backtraces, registers, locals, disassembly, memory map) and prints a structured report. Demonstrates both MI (machine interface) and CLI modes.

## 6. Useful GDB Commands

Quick reference for interactive debugging via `run_debugger_cmd`:

| Command | What it does |
|---------|-------------|
| `bt` | Stack trace of current thread |
| `bt full` | Stack trace with local variables |
| `thread apply all bt` | Stack traces of **all** threads |
| `info threads` | List all threads |
| `info registers` | Display registers |
| `frame N` | Select stack frame N |
| `info locals` | Show local variables in current frame |
| `info args` | Show function arguments in current frame |
| `print expr` | Evaluate expression |
| `x/20i $pc` | Disassemble 20 instructions at crash point |
| `disassemble` | Disassemble current function |
| `x/<N>xb <addr>` | Hex dump N bytes at address |
| `info proc mappings` | Show memory map |
| `info sharedlibrary` | List loaded shared libraries |
| `info proc` | Process info (PID, executable) |

## 7. Try These Examples

**Start simple:**
```bash
ulimit -c unlimited
./build/out/stack-overflow
# Then: analyze_dump core.stack-overflow.<pid>
```

**Try a complex one:**
```bash
./build/out/heap-metadata-corruption
# The off-by-one is in a different function than where it crashes
# Can the analysis trace back to the real bug?
```

**Multi-threaded crash:**
```bash
./build/out/thread-uaf
# Two threads race on the same object
# Check "thread apply all bt" to see what both threads were doing
```
