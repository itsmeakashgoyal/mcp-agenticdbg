# Crash Examples

Seventeen intentional crash programs for testing the TriagePilot MCP server. Each generates a crash dump with full debug symbols so you can practice triage from build to fix.

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
| `format-string-crash` | User data reaches `printf()` as the format string (CWE-134) | Advanced |
| `iterator-invalidation` | `std::vector` growth frees a cached pointer to an element | Advanced |
| `exception-in-destructor-terminate` | Destructor throws mid-unwind &rarr; `std::terminate()` | Advanced |
| `cyclic-refcount-stack-overflow` | Reparent bug creates a cycle; recursive destructor loops forever | Advanced |
| `concurrent-vector-race` | Four threads push_back into one unlocked `std::vector` | Multithreading |
| `lock-order-inversion-deadlock` | Two threads lock two mutexes in opposite order; watchdog aborts the hang | Multithreading |
| `detached-thread-dangling-stack` | Detached thread holds a raw pointer into a returned stack frame | Multithreading |

The `Advanced` tier crashes without any explicit `free()`/`delete` at the
crash site at all -- the bug is in container growth semantics, C++ exception
rules, or graph structure, so the top stack frame alone doesn't explain the
root cause the way it does for the `Simple` tier.

The `Multithreading` tier (alongside `thread-uaf` in `Complex`) requires
correlating *multiple* thread stacks at once -- `info threads` /
`thread apply all bt` -- since no single thread's backtrace explains the
bug in isolation. `lock-order-inversion-deadlock` is unusual in that
nothing actually faults: the "crash" is a watchdog-triggered `abort()` used
to make a hang analyzable, the same technique real services use in
production.

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
