# Crash Examples

Ten intentional crash programs for testing the TriagePilot MCP server. Each generates a crash dump with full debug symbols so you can practice triage from build to fix.

## Pick Your Platform

| Platform | Folder | Guide | Debugger |
|----------|--------|-------|----------|
| **Windows** | [`windows/`](windows/) | [windows/README.md](windows/README.md) | CDB / WinDbg |
| **Linux** | [`linux/`](linux/) | [linux/README.md](linux/README.md) | GDB |
| **macOS** | [`macos/`](macos/) | [macos/README.md](macos/README.md) | LLDB |

Go to your platform's folder and follow the README. Each guide walks you through: **prerequisites -> build -> crash -> analyze** in one page.

## Examples

All crash programs live in [`common/`](common/) and are shared across platforms.

| Example | Crash Type | Difficulty |
|---------|-----------|------------|
| `stack-overflow` | Unbounded recursion exhausts thread stack | Simple |
| `use-after-free` | Dereference freed + recycled heap memory | Simple |
| `double-free` | Free same block twice, corrupt free-lists | Simple |
| `vtable-corruption` | Virtual call on deleted object | Simple |
| `stack-buffer-overrun` | Stack buffer overflow overwrites function pointer | Simple |
| `heap-corruption` | Write past allocation boundary | Simple |
| `deep-callchain-nullptr` | Null deref 12+ frames deep in recursive evaluator | Complex |
| `heap-metadata-corruption` | Off-by-one corrupts heap metadata; crash in `free()` | Complex |
| `multi-inheritance-crash` | Wrong C-style cast + multiple inheritance vtable crash | Complex |
| `thread-uaf` | Multi-threaded use-after-free race condition | Complex |

## How It Works

Every example calls `EnableCrashDumps()` from `common/crashdump.h` at startup:

- **Windows** -- installs an unhandled-exception filter that writes a `.dmp` MiniDump (via `dbghelp.lib`) to a `dumps/` folder next to the executable.
- **Linux / macOS** -- installs signal handlers for `SIGSEGV`, `SIGABRT`, `SIGBUS`, `SIGFPE` and enables unlimited core dumps via `setrlimit`. On macOS, use `gen_core_mac.sh` to capture cores reliably (see [macos/README.md](macos/README.md)).

Debug symbols are embedded in the executables (DWARF on Linux/macOS) or in `.pdb` files next to the `.exe` (Windows), so the debugger automatically resolves function names, source lines, and local variables.

## Directory Layout

```
examples/
  README.md                     <- you are here
  common/                       <- shared source code (all platforms)
    crashdump.h                     cross-platform dump header
    *.cpp                           crash example source files
  windows/                      <- Windows-specific
    README.md                       complete Windows walkthrough
    build.ps1                       MSVC build script
    run-all.ps1                     run all examples and collect dumps
    cdb_triage_demo.py              standalone CDB triage demo
  linux/                        <- Linux-specific
    README.md                       complete Linux walkthrough
    build.sh                        GCC/Clang build script
    gdb_triage_demo.py              standalone GDB triage demo
  macos/                        <- macOS-specific
    README.md                       complete macOS walkthrough
    build.sh                        Clang build script
    gen_core_mac.sh                 generate core dump via lldb
    lldb_triage_demo.py             standalone LLDB triage demo
```
