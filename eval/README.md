# TriagePilot Crash-Triage Eval

A small, reproducible benchmark that answers one question: **when TriagePilot
analyzes a crash dump, does it correctly identify the signal, the faulting
frame, and the source location?**

## Scope (read this before interpreting a score)

This harness grades TriagePilot's **deterministic debugger-grounding layer**
-- `get_crash_info()`, `run_crash_analysis()`, and `locate_faulting_source()`,
the same functions `analyze_dump` calls. It does **not** grade the LLM
root-cause explanation produced by the optional LangGraph path
(`auto_triage_dump`). That prose needs a model judge and an API key to score
fairly, and quality there is bottlenecked by this grounding layer anyway --
an LLM reasoning over a wrong stack trace produces a confidently wrong root
cause. Scoring the foundation first is the correct order of operations; a
judged LLM-quality eval is a natural follow-up once this layer is trusted.

## What's scored

For each of the 17 example crashes under `examples/common/`, per
`ground_truth.py` (minus any examples `run_eval.py`'s `_PLATFORM_EXCLUSIONS`
skips on the current platform -- see "macOS and Windows crash capture"
below):

1. **Signal match** -- did the reported/observed signal match the expected
   one (`SIGSEGV`, `SIGABRT`, ...)?
2. **Frame match** -- does the analysis output mention at least one of the
   expected user-code function names?
3. **Source located** -- did `locate_faulting_source()` map the crash back
   to the correct `.cpp` file in the repo?

Each example scores 0/33/67/100%. The aggregate is the mean over examples
that actually reproduced a crash (see below) -- non-reproductions are
reported separately, not averaged in as failures, since they reflect example
reliability, not TriagePilot's analysis quality.

## Running it

Requires a real debugger on `PATH` (`gdb` on Linux, `lldb` on macOS, `cdb`
on Windows) and a C++ compiler -- `g++`/`clang++` on Linux/macOS, or
`cl.exe` on Windows (run from a Developer Command Prompt so it's on
`PATH`; the harness always uses `cl.exe` there, matching
`examples/windows/build.ps1`'s flags, rather than a MinGW `g++`/`clang++`
that might also be on `PATH`).

```bash
uv sync --extra langgraph   # or: pip install -e .
uv run python eval/run_eval.py
```

Useful flags:

```bash
# Just a couple of examples, while iterating on the harness itself
uv run python eval/run_eval.py --only use-after-free double-free

# Keep the temp run directories (binaries + core files) for manual inspection
uv run python eval/run_eval.py --keep-cores
```

Results are printed to stdout and written to `eval/results.md`.

### Core dumps on Linux

Ubuntu (and other distros running `apport`) redirect core dumps to a crash
reporter instead of writing a `core` file next to the binary. If every
example reports "not reproduced", check:

```bash
cat /proc/sys/kernel/core_pattern
```

If it points to a pipe (`|...`) rather than a plain `core` pattern, fix it
for the session:

```bash
echo core | sudo tee /proc/sys/kernel/core_pattern
ulimit -c unlimited
```

See `docs/TROUBLESHOOTING.md` for the macOS equivalent (`ReportCrash`
intercepts signals; use `examples/macos/gen_core_mac.sh`).

## Known example reliability

Direct testing on Ubuntu 22.04 **aarch64** originally surfaced three examples
that did not reproduce their crash in 5/5 attempts, plus one whose signal is
architecture-dependent. The three non-reproducing examples have since been
hardened (see `ground_truth.py` notes for each) to remove their dependence on
allocator/scheduler luck:

| Example | Fix | Result after hardening |
|---|---|---|
| `heap-corruption` | Corrupts a neighboring chunk's own size header at the exact glibc-computed offset (`malloc_usable_size`) instead of a fixed poison overwrite that may not land anywhere validated. | Crashed 20/20 direct test runs on Ubuntu 22.04 x86_64 glibc 2.35. |
| `heap-metadata-corruption` | Same technique, applied to a small tracking record deliberately allocated adjacent to the packet buffer, on top of the original off-by-one accounting bug. | Crashed 15/15 direct test runs. |
| `thread-uaf` | Replaced the `usleep()` timing guess with an explicit atomic request-counter handshake between worker and watchdog, and padded `Session` past glibc's mmap threshold so `free()` truly unmaps it (small-object UAFs often don't fault at all on glibc). | Crashed 20/20 direct test runs. |

One example still has an architecture-dependent (not flaky) signal:

| Example | Finding |
|---|---|
| `stack-buffer-overrun` | Crashed reliably, but with `SIGBUS` on aarch64 vs. the `SIGSEGV` implied by the original (Windows-oriented) header comment -- ARM enforces stricter alignment faults on indirect jumps to garbage addresses than x86_64 does. `detached-thread-dangling-stack` has the same aarch64/x86_64 signal split. Both list both signals in `expected_signals`. |

All seventeen examples now reproduce their documented signal reliably in
direct testing; none of the original non-reproduction findings reflected a
TriagePilot bug -- it was example-program determinism.

## macOS and Windows crash capture

Crash capture works differently per platform because crashdump.h's two
implementations write different artifacts:

- **macOS** -- a plain `subprocess.run()` + `ulimit -c unlimited` doesn't
  reliably produce a core, because ReportCrash intercepts the signal
  first (the same issue `examples/macos/gen_core_mac.sh` works around).
  The harness runs each binary under
  `lldb --batch -o run --one-line-on-crash "process save-core ..."`
  instead, exactly like that script.
- **Windows** -- runs each example under `cdb.exe` itself
  (`-g -G -hd -c "g;.dump /ma <path>;q"`) rather than trusting
  crashdump.h's in-process `SetUnhandledExceptionFilter` handler, so a
  debugger is already attached when the crash happens -- see the
  "Windows" section below for why that specifically matters.

Every non-reproduction carries the crashing process's own last-attempt
output (crashdump.h's prints, or a bare exit code if nothing printed) in
`results.md`'s Notes section under a collapsible "last run's output"
block, so findings like the ones below were root-caused from the CI
artifact, not guessed at.

### macOS: was 14/17 at 64%, now 16/16 reproduced

Root-caused directly on real macOS hardware (arm64, macOS 15.7) and,
subsequently, real macOS CI (arm64, macOS 26) -- these are two *different*
lldb builds on two different OS versions, and each surfaced its own
findings; don't assume a fix verified on one holds on the other without
checking. At least four separate, unrelated issues were dragging the
score down, not one:

**1. Two examples never crashed at all.** `heap-metadata-corruption`,
`thread-uaf`, and `iterator-invalidation` all ran to completion with exit
status 0, confirmed from each one's captured output. All three rely on a
freed allocation being reused (or at least still faulting when touched
through the stale pointer) rather than sitting untouched in a
per-size-class free list; that behavior is allocator-specific and doesn't
hold on macOS's libmalloc the way it does on glibc.

- `thread-uaf` and `iterator-invalidation` are fixed: `Session` (thread-uaf)
  now defines its own `operator new`/`operator delete` backed directly by
  `mmap()`/`munmap()`, and `MetricsBuffer`'s `std::vector<Sample>`
  (iterator-invalidation) now uses a custom `DirectMapAllocator` backed by
  `mmap`/`VirtualAlloc`, bypassing the platform malloc's heuristics
  entirely. A first mmap/munmap-only version of the allocator *still*
  didn't reproduce reliably for iterator-invalidation -- allocation-address
  tracing showed the kernel handing the just-freed address straight back
  out to the next similarly-sized `mmap()` a couple of vector growths
  later, silently making the stale pointer valid again. Remapping the
  freed range `PROT_NONE` (Windows: `MEM_DECOMMIT`) instead of releasing it
  keeps the address permanently reserved so it can never be reused.
  Confirmed 10/10 (thread-uaf) and 15/15 (iterator-invalidation) direct
  runs; see `ground_truth.py` notes for each.
- `heap-metadata-corruption` is excluded from the eval on macOS entirely
  (`run_eval.py`'s `_PLATFORM_EXCLUSIONS`) rather than left as a
  non-reproduction, since it's believed unfixable without deep,
  version-fragile reverse-engineering of libmalloc's internal chunk
  layout: the technique needs `conn_record` to sit immediately *after*
  `buf` in memory so stomping `buf`'s computed usable-size offset lands on
  `conn_record`'s own header. Tested directly: on this macOS allocator,
  the two allocations don't even land in that relative order
  (`conn_record` was allocated **before** `buf`, at a lower address) --
  the whole technique's premise doesn't hold, unlike the glibc case it was
  designed around. The 16 remaining examples are all run and all
  reproduce.

**2. Every reproduced crash was scored "signal MISS."** Even before fixing
the above, all 14 previously-reproducing examples showed the correct frame
and source but a mismatched signal (`64%` aggregate is almost exactly the
2/3 you'd get from frame+source matching with signal always missing).
Root cause: lldb's `debugserver` on Apple Silicon reports the raw AArch64
`ESR_EL1.EC` hardware exception class as the stop reason (e.g.
`ESR_EC_DABORT_EL0`) instead of `EXC_BAD_ACCESS`/`signal SIGSEGV` the way
x86_64 lldb does, and a signal-delivered abort (`SIGABRT` via `abort()`)
carries *no* stop-reason at all when lldb loads a saved core file rather
than live-attaching -- confirmed empirically that a genuine `SIGSEGV` and
a genuine `SIGBUS` both report the identical `ESR_EC_DABORT_EL0`, and a
`double-free` core's `process status` just says "Process 0 stopped" with
nothing after it, even though the backtrace clearly shows the `abort()` /
`malloc_zone_error` / `free_tiny_botch` chain. Fixed in
`memory/signature.py`'s `_extract_exception_type()`: known `ESR_EC_*`
classes now map to the equivalent POSIX signal, and an unmatched abort
backtrace (`` `abort +`` frame) falls back to `SIGABRT`. Separately, the
eval harness itself (`run_eval.py`) was scoring signal match against a
raw substring search over debugger text rather than calling
`extract_crash_signature()` -- the function this eval is actually meant to
be grading -- so the fix above didn't move the score until that was
corrected too.

Not a TriagePilot analysis-quality bug in the sense of getting frames or
source wrong -- but the ESR_EC gap was a real product bug (it also broke
crash-signature normalization and auto-tagging in the persistent memory
system for any Apple Silicon user, not just the eval), and the two
example fixes are the same category of example-program determinism the
aarch64 findings above already document, just closed instead of left
open.

**3. On macOS 26 specifically, three more examples reported "signal
MISS."** `use-after-free`, `double-free`, and `heap-corruption` all
reproduced fine but scored a mismatched signal on real macOS 26 CI --
different symptom, different root cause from #2 above. A newer libmalloc
("xzone malloc", judging by the `mfm_alloc`/`mfm_free`/`xzm_*` symbol
names) now embeds a `brk` instruction as an inline hardware assertion in
its fast malloc/free paths, catching heap-safety violations *before* any
ordinary fault or `abort()` would occur -- reported as
`ESR_EC_BRK_AARCH64`, a code that isn't in the `_LLDB_ESR_EC_TO_SIGNAL`
table added for finding #2. Confirmed from CI's captured
crash_info+analysis (see the "Signal/frame/source mismatches" report
section this investigation added to `run_eval.py`): all three land
inside `libsystem_malloc.dylib` at the trap point, and `double-free`'s
crash message is literally *"BUG IN LIBMALLOC: malloc assertion
'main_address' failed"*. Fixed by mapping `ESR_EC_BRK_AARCH64` to
`SIGABRT` *only* when `libsystem_malloc.dylib` appears near the stop
reason (a breakpoint trap from unrelated user code -- a compiler-inserted
`__builtin_trap()`, an ASan check -- reports the same generic ARM64
exception class and should NOT be guessed as SIGABRT). `use-after-free`'s
`expected_signals` gains `SIGABRT` alongside `SIGSEGV` since this is what
genuinely happens on this allocator now: the stale write's corruption is
only caught later, inside a *subsequent* `malloc()` call's internal
consistency check, not by the stale write itself faulting.

**4. `exception-in-destructor-terminate` lost its backtrace entirely on
macOS 26.** Frame and source both miss (33% instead of 100%) -- not a
signal-detection issue like the others; lldb's unwind stops dead at
``libc++abi.dylib`demangling_terminate_handler()`` with no user-code frame
on any thread. Investigated and ruled out core-completeness as the cause:
`process save-core`'s default style already matches `--style
modified-memory` (confirmed byte-identical locally), and `--style full`
produces a ~5 GB core per example on this machine -- utterly impractical
across 16 examples in CI, and wouldn't even be expected to help, since
system libraries resolve from disk on the same machine regardless of
core style. This looks like a genuine lldb/libc++abi compact-unwind gap
specific to this double-exception-during-destructor-unwind call chain on
macOS 26's build of libc++abi -- not fixed here (see `ground_truth.py`'s
notes for this example). Worth another look if a future macOS/Xcode
update changes lldb's unwinder, or if someone wants to dig into whether
an lldb setting can force DWARF-based unwinding through this specific
frame.

### Windows: was 0/16, now 16/16 reproduced at 100% (real Windows 11, real cdb 10.0.29617.1000/cl.exe 19.42)

Every non-reproduction's captured output pointed to the same bug once
diagnosed: `crashdump.h`'s Windows `CrashDumpHandler()` computed the
dump's base filename from a buffer it had *already null-terminated to an
empty string one line earlier*, so every dump was actually written
successfully as `<dumpDir>\.{pid}.dmp` (no program name, leading dot) --
confirmed by output like `[crashdump] Dump written: ...\dumps\.8916.dmp`
for `use-after-free`. `eval/run_eval.py`'s `glob.glob("*.dmp")` silently
skips leading-dot filenames as hidden (a convention baked into Python's
`glob` module on every platform, not just POSIX), so it never saw them --
while `run-all.ps1`'s `Get-ChildItem -Filter "*.dmp"` has no such
convention and found them fine, which is why `windows-cdb-smoke-test`
never caught this. Fixed in `crashdump.h`.

That single fix took Windows from 0/16 to 8/16 -- including
`stack-overflow`, which an earlier version of this doc guessed had a
*separate* problem (the handler itself faulting from stack exhaustion,
based on one retry attempt's captured output showing no "Dump written"
line). That guess was wrong: it was the same empty-basename bug all
along, just caught mid-retry on an attempt whose diagnostic happened to
get captured before the print. Worth noting since it's a reminder to
trust the next real result over a plausible-sounding theory.

That fix alone took Windows from 0/16 to 8/16, then 9/16 once
`cyclic-refcount-stack-overflow`'s CDB-session-timeout fix (below) landed.
Closing the rest of the gap turned out to need five more fixes, all
confirmed directly against a real Windows 11 box (Visual Studio 2022's
`cl.exe` 19.42.34440, WinDbg's `cdb.exe` 10.0.29617.1000 installed via
`winget install Microsoft.WinDbg`) rather than guessed at or left for CI to
discover -- **16/17 examples now run** (`thread-uaf` is fixed below;
`heap-metadata-corruption` is excluded, same as macOS) and **all 16
reproduce at 100% aggregate accuracy**, up from 9/16 at 70%. Stable across
repeated runs (verified 5 consecutive full passes, all 16/16 at 100%).

**1. `__fastfail` crashes ($5$ examples: `double-free`, `heap-corruption`,
`concurrent-vector-race`, `exception-in-destructor-terminate`,
`lock-order-inversion-deadlock`) never reached `SetUnhandledExceptionFilter`
at all.** All five are Windows `__fastfail` codes (`STATUS_HEAP_CORRUPTION`
`0xC0000374` or the `abort()`-based `STATUS_STACK_BUFFER_OVERRUN`
`0xC0000409`) -- by design, `__fastfail` bypasses normal SEH dispatch,
including a handler registered in the target process itself, unless a
debugger is already attached, specifically so a corrupted-heap process
can't have its termination hijacked by its own (possibly also corrupted)
handler. Fixed by rewriting `_run_until_crash_windows` to run each example
under `cdb.exe` itself (`-g -G -hd -c "g;.dump /ma <path>;q"`, the exception
first-chance analogue of macOS's `lldb --batch ... --one-line-on-crash`)
instead of trusting the target's own in-process handler -- a debugger *is*
notified of `__fastfail` exceptions even when a standalone process isn't, so
cdb can write the dump itself before the process would otherwise terminate
unseen. Confirmed live: this alone reproduced all five.

**2. `!analyze -v` was silently failing (and poisoning every section after
it) on every single example, not just the five above** -- the reason
signal/source scoring was weak even for examples that *did* reproduce.
Root cause: `run_crash_analysis()`'s internal `_try()` helper always
substituted `self.timeout` for a bare `None`, which defeated
`send_command()`'s own activity-based patience for commands in
`SLOW_COMMAND_PREFIXES` (`!analyze`, `~*kb`, `vertarget`, ...) -- confirmed
live that `!analyze -v` can legitimately take **~159 seconds** (mostly a
first-use online WER bucket-ID lookup; `Analysis.IO.Write.Mb` alone was 90),
comfortably past the 90s ceiling this eval passed as the session timeout,
and once it hard-failed at that ceiling every later section (`Registers`,
`Loaded Modules`, `Target Info`) failed too. Fixed in
`backends/cdb.py`'s `_try()` (pass `cmd_timeout` through as-is instead of
pre-resolving `None`) and bumped the eval's own session timeout to 200s for
headroom above the observed 159s.

**3. CDB's exception-type extraction has no fallback when `!analyze -v`'s
`EXCEPTION_RECORD`/`BUGCHECK_STR` block isn't present**, and by its own
test contract (`test_extract_cdb_signature`) returns Windows-native labels
("ACCESS_VIOLATION") rather than POSIX names anyway, so it was never going
to satisfy `ground_truth.py`'s `SIGSEGV`/`SIGABRT` entries for this backend
even when fix #2 above makes `!analyze -v` reliable. The eval's own
capture-time signal derivation (deleted from an earlier draft of the
`cdb.exe`-launch rewrite in #1, on the mistaken assumption
`extract_crash_signature` would cover it) is what actually needs to carry
this: `_run_until_crash_windows` now maps the NTSTATUS code from cdb's own
stop-reason line to the nearest POSIX signal
(`_WINDOWS_NTSTATUS_TO_SIGNAL`), taking the *last* `code XXXXXXXX` match in
cdb's transcript rather than the first (confirmed live: cdb's own startup
chatter can print an earlier, unrelated code -- observed `STATUS_BREAKPOINT`
`0x80000003` from the loader breakpoint `-g` auto-continues past -- before
the real crash).

**4. `concurrent-vector-race`'s genuine data race can manifest as *either*
a plain access violation *or* `STATUS_BREAKPOINT` (`0x80000003`)** -- confirmed
live across repeated runs (mostly `SIGSEGV`, occasionally
`!analyze -v`'s own `Failure.Bucket` reading
`HEAP_CORRUPTION_ACTIONABLE_..._DOUBLE_FREE_80000003_...`): Windows'
page-heap/heap-corruption detection breaking in via a breakpoint exception
rather than a `__fastfail` code, for what's fundamentally the same class of
bug `double-free.cpp`/`heap-corruption.cpp` trigger. Added to the same
NTSTATUS map from #3, mapped to `SIGABRT` (already one of this example's
`expected_signals`).

**5. Source localization had three separate, real bugs** surfaced by
examples whose faulting *frame* is a library/CRT function rather than user
code -- not Windows-specific mechanisms, but only exercised by CDB's
`SYMBOL_NAME`/stack-frame search path (`locate_faulting_source()`'s Level
2/3a in `debugger_tools.py`), so they never showed up scoring GDB/LLDB's
richer `at file:line` debug info on Linux/macOS:
   - `_find_function_in_repo()` accepted a `module_hint` but only used it
     for *display*, never to rank matches -- so a generic name defined in
     many files (every example's own `main`) could rank the wrong file's
     definition into the truncated `max_show=3` list instead of the real
     one. Worse, the substring check it *did* do for ranking
     (`module_hint in filepath`) can never match on Windows anyway: a
     PE/COFF module name can't contain a hyphen, so CDB reports MSVC-built
     `double-free.exe` as module `double_free`, and `"double_free" in
     ".../double-free.cpp"` is false. Fixed: strip `-`/`_` from both sides
     before comparing.
   - Destructors defined inline in the class body (`~Foo() {`, no
     `ClassName::` qualifier) never matched either of the two "definition"
     regexes -- both require a word-boundary or `::` immediately before the
     name, and `~` is itself non-word, so there's never a `\w`/`\W`
     transition for `\b` to anchor on between it and preceding whitespace.
     Confirmed live: `exception-in-destructor-terminate.cpp`'s inline
     `~ScopedTransaction`. Fixed with a dedicated pattern for names
     starting with `~`.
   - A function name that's a library/CRT call (`abort`, `free`, `memcpy`,
     ...) rather than something the user defines can still text-match a
     *call site* elsewhere in the repo -- confirmed live that a search for
     `abort` (the top-of-stack frame for a `__fastfail` example) matched a
     bare `abort();` call in `lock-order-inversion-deadlock.cpp`, and
     separately matched a *comment* mentioning `abort()` before that was
     fixed with basic C/C++ comment-stripping. Static CRT linking (`/MT`)
     also means module-name filtering alone can't rule these out --
     `double_free!abort` attributes CRT code to the user's own module, not
     `ucrtbase`. Fixed with a skip-list of well-known runtime function
     names and system module names (mirrors the skip-list
     `_extract_gdb_functions()` already used for the same reason on the GDB
     path) -- plus excluding `.venv`/`site-packages`-style dependency
     directories from the search entirely, the Python equivalent of the
     `node_modules` exclusion already there, after a search for `abort`
     landed on a vendored `mypyc` runtime file inside this repo's own
     `.venv`.

**`thread-uaf` is no longer excluded on Windows.** It used raw POSIX
pthreads (`<pthread.h>`, `sched_yield()`), which have no MSVC equivalent;
ported to `std::thread`/`std::this_thread::yield()` (same as
`lock-order-inversion-deadlock.cpp`, `concurrent-vector-race.cpp`,
`detached-thread-dangling-stack.cpp` already did) and its custom
`operator new`/`delete` to `VirtualAlloc`/`VirtualFree(MEM_DECOMMIT)`
(mirroring `iterator-invalidation.cpp`'s `DirectMapAllocator`). Confirmed
live: reproduces reliably, `100%` score.

**`heap-metadata-corruption` is excluded on Windows, same as macOS.** Its
deterministic-reproduction technique (stomping a neighboring chunk's own
size header at the exact offset `malloc_usable_size()` computes) is guarded
behind `HMC_GLIBC_TECHNIQUE`, on only for Linux+glibc; without it, the
underlying off-by-one bug is real but not reliably fatal (confirmed live:
exits 0 every attempt on real Windows). A Windows-specific equivalent would
need reverse-engineering the NT Heap/LFH chunk layout, which -- unlike
glibc's plain-text header -- has been cookie-obfuscated since Windows 8;
the same class of fragile, version-specific problem that got this example
excluded on macOS rather than attempted. See `_PLATFORM_EXCLUSIONS` in
`eval/run_eval.py`.

One earlier finding is now fully resolved rather than a remaining gap:
`cyclic-refcount-stack-overflow` used to reproduce the crash and write a
dump fine, but then fail with `CDBError: CDB initialization timed out` --
a *different* kind of failure from the others (it counted as "no" in the
table with no diagnostic block, since `run_until_crash` succeeded and the
failure happened afterwards, in `create_session()`). Its
`MiniDumpWithFullMemory` dump plus CDB's symbol loading for a
deeply-templated shared_ptr/STL recursion apparently needs more than a 30s
session timeout on a loaded runner, even though gdb/lldb open the
equivalent dump for the same example in well under that. Bumped the eval's
session timeout (30s -> 90s -> 200s, the last bump for finding #2 above);
reproduces reliably now, confirmed across 5 consecutive full runs.

## CI

A `.github/workflows/ci.yml` job (`eval`) runs this harness on every push to
`master` using a real `ubuntu-latest` runner (full internet access, so
`gdb` installs cleanly and there's no sandboxed-core-dump restriction).
Sibling `macos-eval` and `windows-eval` jobs run the same harness with
`lldb`/`cdb` on `macos-latest`/`windows-latest`. See those jobs for the
exact setup steps this README assumes.
