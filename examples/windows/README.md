# Windows -- Build, Crash, and Analyze

Complete walkthrough for analyzing crash dumps on Windows using CDB/WinDbg.

## 1. Prerequisites

- **Visual Studio Build Tools** (2019 or later) -- provides `cl.exe` and `link.exe`
- **Windows SDK** -- provides `dbghelp.lib` (used by `crashdump.h` to write MiniDumps)
- **CDB** -- installed with the Windows SDK under _Debugging Tools for Windows_

Verify your setup:

```powershell
# Open "Developer Command Prompt for Visual Studio" or "Developer PowerShell", then:
cl.exe /?         # should print MSVC version
cdb.exe -version  # should print CDB version
```

## 2. Build

From a **Developer Command Prompt** (so `cl.exe` is on PATH):

```powershell
cd examples\windows
.\build.ps1
```

This compiles all 10 examples from `..\common\` with full debug symbols:
- `/Zi` -- generate PDB debug info
- `/Od` -- no optimization (clean stack traces)
- `/GS-` -- disable stack cookies (show raw corruption)

Output: `build\out\*.exe` + `build\out\*.pdb`

## 3. Generate Crash Dumps

Run all examples and collect dumps:

```powershell
.\run-all.ps1
```

Or run a single example:

```powershell
.\run-all.ps1 -Name use-after-free
```

Each program crashes and writes a `.dmp` file to `build\out\dumps\`. The script prints a summary table showing which dumps were created.

## 4. Analyze with TriagePilot (MCP Tools)

Start TriagePilot and use the MCP tools from your AI assistant:

```
# Full crash analysis (runs !analyze -v, backtraces, registers, modules)
analyze_dump  examples\windows\build\out\dumps\use-after-free.exe.1234.dmp

# Run specific CDB commands
run_debugger_cmd  .ecxr
run_debugger_cmd  kb
run_debugger_cmd  ~*kb
run_debugger_cmd  r
run_debugger_cmd  dv /t
run_debugger_cmd  !analyze -v

# Close the session when done
close_dump  examples\windows\build\out\dumps\use-after-free.exe.1234.dmp
```

## 5. Analyze with Python Demo

Run the standalone CDB triage demo directly:

```powershell
python cdb_triage_demo.py build\out\dumps\use-after-free.exe.1234.dmp ^
    --symbols build\out ^
    --image build\out
```

This calls every inspection method (crash summary, all-thread backtraces, registers, locals, disassembly, memory map) and prints a structured report.

## 6. Useful CDB Commands

Quick reference for interactive debugging via `run_debugger_cmd`:

| Command | What it does |
|---------|-------------|
| `.ecxr` | Switch to the exception context (faulting thread/registers) |
| `kb` | Stack trace of current thread |
| `~*kb` | Stack traces of **all** threads |
| `r` | Display registers |
| `.frame N` | Select stack frame N |
| `dv /t` | Show local variables with types (in current frame) |
| `?? expr` | Evaluate a C++ expression |
| `u @rip L20` | Disassemble 20 instructions at crash point |
| `uf <function>` | Disassemble entire function |
| `db <addr> L<len>` | Hex dump memory |
| `!address <addr>` | Show memory region info (heap/stack/mapped) |
| `!analyze -v` | Full automated crash analysis |
| `!heap -s` | Heap summary (useful for heap corruption) |
| `!locks` | Show lock state (useful for deadlocks) |
| `vertarget` | OS version, process info |
| `!peb` | Process Environment Block |
| `lm` | List loaded modules |

## 7. Try These Examples

**Start simple:**
```powershell
.\run-all.ps1 -Name stack-overflow
# Then: analyze_dump build\out\dumps\stack-overflow.exe.<pid>.dmp
```

**Try a complex one:**
```powershell
.\run-all.ps1 -Name deep-callchain-nullptr
# The null deref is 12+ frames deep -- can the analysis find the root cause?
```

**Multi-threaded crash:**
```powershell
.\run-all.ps1 -Name thread-uaf
# Two threads race on the same object -- check ~*kb to see both threads
```
