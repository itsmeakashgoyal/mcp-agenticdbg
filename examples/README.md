# Crash Examples

Six intentional crash programs that generate crash dumps and debug symbols for testing the TriagePilot MCP server.

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

Each example includes `crashdump.h`, which provides cross-platform crash dump support:
- **Windows:** Installs an unhandled-exception filter that writes a full MiniDump (`.dmp`).
- **Linux / macOS:** Installs signal handlers and enables core dumps via `setrlimit`.

## Prerequisites

### Windows
- **Visual Studio Build Tools** (2019 or later) — specifically `cl.exe` and `link.exe`
- **Windows SDK** — provides `dbghelp.lib` (used by `crashdump.h`)
- *Alternatively*, use **Clang for Windows** with `clang-cl` or `clang++`

### Linux
- **GCC** (`g++`) or **Clang** (`clang++`)
- Core dumps enabled: `ulimit -c unlimited`

### macOS
- **Xcode Command Line Tools** or **Clang** (pre-installed on macOS)
- Core dumps enabled: `ulimit -c unlimited`

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

## Run

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

### Linux / macOS

First, enable core dumps:

```bash
ulimit -c unlimited
```

Then run an example:

```bash
./build/out/stack-overflow
```

Core dumps will be written to the current directory, `/var/crash/`, or as configured by `/proc/sys/kernel/core_pattern` (Linux).

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

### macOS (core dump)
```
analyze_dump   /cores/core.12345
run_debugger_cmd   bt all
run_debugger_cmd   thread list
close_dump     /cores/core.12345
```

Debug symbols are embedded in the executables (DWARF on Linux/macOS) or in `.pdb` files alongside the `.exe` (Windows), so the debugger will automatically resolve function names, source lines, and local variables.
