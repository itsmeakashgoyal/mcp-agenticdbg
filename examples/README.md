# Crash Examples

Ten intentional crash programs that generate crash dumps and debug symbols for testing the TriagePilot MCP server.

Supported platforms: **Windows** (MSVC or Clang), **Linux** (GCC or Clang), **macOS** (Clang).

## Examples

| File | Crash Type | Windows Exception / POSIX Signal |
|---|---|---|
| `stack-overflow.cpp` | Unbounded recursion exhausts the thread stack | `0xC00000FD` / `SIGSEGV` |
| `use-after-free.cpp` | Dereference a pointer into freed & recycled heap memory | `0xC0000005` / `SIGSEGV` |
| `double-free.cpp` | Free the same block twice, corrupting heap free-lists | Heap corruption / `SIGABRT` |
| `vtable-corruption.cpp` | Call a virtual method on a deleted + overwritten object | `0xC0000005` / `SIGSEGV` |
| `stack-buffer-overrun.cpp` | Overflow a stack buffer to overwrite an adjacent function pointer | `0xC0000005` / `SIGSEGV` |
| `heap-corruption.cpp` | Write past an allocation boundary, corrupting heap chunk metadata | Heap corruption / `SIGABRT` |
| `deep-callchain-nullptr.cpp` | Null dereference 12+ frames deep in a recursive evaluator | `0xC0000005` / `SIGSEGV` |
| `heap-metadata-corruption.cpp` | Off-by-one corrupts heap metadata; crash occurs inside `free()` | Heap corruption / `SIGABRT` |
| `multi-inheritance-crash.cpp` | Wrong C-style cast across multiple inheritance triggers vtable crash | `0xC0000005` / `SIGSEGV` |
| `thread-uaf.cpp` | Multi-threaded use-after-free: two threads race on the same object | `0xC0000005` / `SIGSEGV` |

Each example includes `crashdump.h`, which provides cross-platform crash dump support:
- **Windows:** Installs an unhandled-exception filter that writes a full MiniDump (`.dmp`).
- **Linux:** Installs signal handlers and enables core dumps via `setrlimit`.
- **macOS:** Installs signal handlers; use `gen_core_mac.sh` to reliably capture the core (see below).

## Prerequisites

### Windows
- **Visual Studio Build Tools** (2019 or later) — specifically `cl.exe` and `link.exe`
- **Windows SDK** — provides `dbghelp.lib` (used by `crashdump.h`)
- *Alternatively*, use **Clang for Windows** with `clang-cl` or `clang++`

### Linux
- **GCC** (`g++`) or **Clang** (`clang++`)
- Core dumps enabled: `ulimit -c unlimited`
- Check `core_pattern`: `cat /proc/sys/kernel/core_pattern`

### macOS
- **Xcode Command Line Tools**: `xcode-select --install`
- `gen_core_mac.sh` (included) handles core generation — no `/cores` setup required.

> **Why `gen_core_mac.sh`?**
> On macOS 12+, the system's `ReportCrash` agent intercepts fatal signals before the kernel
> writes to `/cores`, even with `ulimit -c unlimited` and a world-writable `/cores`.
> `gen_core_mac.sh` runs the binary under `lldb` (which intercepts the crash first) and uses
> `process save-core` to write the core. This works reliably on all modern macOS versions.

## Build

### Windows (MSVC)

Open a **Developer Command Prompt for Visual Studio** (or Developer PowerShell) and run:

```powershell
cd examples
.\build.ps1
```

### Windows (Clang)

```powershell
clang++ -g -O0 -o build\out\stack-overflow.exe stack-overflow.cpp
```

### Linux / macOS

```bash
cd examples
chmod +x build.sh
./build.sh
```

Notes:
- On macOS, `build.sh` prefers `clang++` by default.
- Override compiler with `CXX=clang++ ./build.sh` (or `CXX=g++ ./build.sh`).

Output for all platforms: executables with debug symbols in `build/out/`.

### Build flags

| Platform | Flag | Purpose |
|---|---|---|
| MSVC | `/Zi` | Emit debug information (generates PDB) |
| MSVC | `/Od` | Disable optimisation |
| MSVC | `/MT` | Static CRT link |
| MSVC | `/GS-` | Disable stack cookies for raw corruption |
| GCC/Clang | `-g` | Include DWARF debug information |
| GCC/Clang | `-O0` | Disable optimisation |

## Run & Generate Core Dumps

### Windows

Execute all examples and collect dumps:

```powershell
.\run-all.ps1
```

Or run a single example:

```powershell
.\run-all.ps1 -Name stack-overflow
```

Dumps are written to `build\out\dumps\`.

### Linux

Enable core dumps, then run an example directly:

```bash
ulimit -c unlimited
./build/out/use-after-free
```

Core dumps are written to the current directory, `/var/crash/`, or wherever
`/proc/sys/kernel/core_pattern` points. Check it with:

```bash
cat /proc/sys/kernel/core_pattern
```

Common patterns:
- `core` → current directory as `core` or `core.<pid>`
- `|/usr/share/apport/apport %p ...` → Ubuntu Apport; dumps go to `/var/crash/`

### macOS

Use `gen_core_mac.sh` — it runs the binary under lldb and saves the core automatically:

```bash
cd examples
chmod +x gen_core_mac.sh

# Default: core written to build/out/core.<example-name>
./gen_core_mac.sh use-after-free

# Optional: specify output path
./gen_core_mac.sh stack-overflow /tmp/stack-overflow.core
```

The script prints the core path on success, e.g.:
```
Core dump written to: build/out/core.use-after-free
```

> Running the binary directly will likely produce a `.crash` report in
> `~/Library/Logs/DiagnosticReports/` instead of a binary core file — use
> `gen_core_mac.sh` to get an LLDB-compatible core.

## Analyze with TriagePilot

Once you have a dump file, use the MCP tools:

### Windows (.dmp)
```
analyze_dump   build\out\dumps\stack-overflow.exe.12345.dmp
run_debugger_cmd   .ecxr; kv
run_debugger_cmd   !analyze -v
close_dump     build\out\dumps\stack-overflow.exe.12345.dmp
```

### Linux (core dump)
```
analyze_dump   core.12345
run_debugger_cmd   bt full
run_debugger_cmd   info threads
close_dump     core.12345
```

### macOS (core dump via gen_core_mac.sh)
```
analyze_dump   examples/build/out/core.use-after-free
run_debugger_cmd   bt all
run_debugger_cmd   thread list
close_dump     examples/build/out/core.use-after-free
```

Or run the standalone LLDB triage demo:

```bash
python examples/lldb_triage_demo.py examples/build/out/core.use-after-free \
    --image examples/build/out/use-after-free
```

Debug symbols are embedded in the executables (DWARF on Linux/macOS) or in `.pdb` files
alongside the `.exe` (Windows), so the debugger will automatically resolve function names,
source lines, and local variables.
