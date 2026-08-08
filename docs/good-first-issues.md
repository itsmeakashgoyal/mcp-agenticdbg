# Draft issues to file

These are ready to paste into GitHub's "New issue" form (Settings →
Issues → New issue, or the repo's Issues tab). `gh issue create` can't
file them directly from this environment — the authenticated account is
an Enterprise Managed User, and GitHub blocks EMU accounts from the
`createIssue` GraphQL mutation on repos outside their enterprise. The
web UI doesn't always hit the same restriction.

For each: create the issue, paste the title and body below, and apply
the listed label(s) (`good first issue` and `help wanted` already exist
on this repo).

---

## 1. Automate `docs/eval-results.svg` and README table generation from eval results

**Label:** `good first issue`

### Problem

`docs/eval-results.svg` and the results table in `README.md` (the
"Latest results" section) are hand-edited to reflect each backend's
reproduction count and accuracy. This drifted stale multiple times in a
single session of manual updates — e.g. the macOS row sat at "14/17,
64%" long after fixes landed, and the Windows row silently drifted from
8/16 to 9/16 between CI runs without anyone noticing until someone
happened to check the raw job logs.

### Proposal

Write a small script (e.g. `scripts/update_eval_badge.py`) that:

1. Parses the three `eval/results.md` outputs (or the CI job summaries /
   `eval-results` artifacts from `.github/workflows/ci.yml`'s `eval`,
   `macos-eval`, and `windows-eval` jobs).
2. Regenerates `docs/eval-results.svg`'s three rows (bar width =
   reproduction rate, label = accuracy) with the exact numbers.
3. Optionally regenerates the markdown table in `README.md` between two
   HTML comment markers.

Nice-to-have: wire it into CI as a job that runs after all three eval
jobs complete, diffs the regenerated SVG/table against what's
committed, and fails (or opens a PR) if they've drifted.

### Where to look

- `docs/eval-results.svg` — the current hand-authored SVG (see its
  bar-width-to-reproduction-rate math in the existing rows)
- `README.md`'s "Evaluation" section — the table this should also update
- `eval/run_eval.py`'s `render_results_md` — the source of truth for
  what a results.md looks like
- `.github/workflows/ci.yml` — the three eval jobs and their artifact
  names (`eval-results`, `eval-results-macos`, `eval-results-windows`)

### Why this is a good first issue

Self-contained, no risk to the actual triage logic, and the "before"
state (a stale SVG) is easy to reproduce and verify against.

---

## 2. Add more crash-type demo GIFs under `docs/demos/`

**Label:** `good first issue`

### Problem

`docs/demos/` currently has terminal-recording demos for 3 of the 17
example crash programs (`use-after-free`, `double-free`, `thread-uaf`).
More would give better coverage of TriagePilot's range — e.g. a vtable
corruption, an STL iterator invalidation, a deadlock, or a stack
overflow.

### Proposal

Follow `docs/demos/README.md`'s "Adding a new one" section exactly:

1. Build and crash another example from `examples/common/` (see
   `examples/macos/gen_core_mac.sh` or `eval/run_eval.py`'s
   `_run_until_crash_macos` for the technique).
2. Run it through TriagePilot's real analysis path (see
   `eval/run_eval.py`'s `evaluate_example` for the exact call sequence)
   and copy the genuine signal/frame/source/backtrace output into a new
   `docs/demos/src/<name>/analysis.md`.
3. Copy `docs/demos/src/use-after-free/run_demo.sh` and `demo.tape` as a
   starting point, adjust the narration to match the real facts in
   `analysis.md`, and render with `vhs` + `glow`.
4. Add a row to the table in `docs/demos/README.md` and link the new
   GIF from `README.md`.

### Ground rule

Keep the analysis content real — the whole point of these demos is that
the output is grounded in an actual debugger session, not written copy.
Don't fabricate a signal, frame, or source snippet that the debugger
didn't actually produce.

### Why this is a good first issue

Fully self-contained, doesn't touch any application code, and the
process is already documented step-by-step with a working example to
copy from.

---

## 3. Verify iterator-invalidation's DirectMapAllocator fix on Windows CI

**Label:** `good first issue`

### Problem

`examples/common/iterator-invalidation.cpp` was hardened this session to
fix a macOS-specific non-reproduction: `MetricsBuffer`'s
`std::vector<Sample>` now uses a custom `DirectMapAllocator` backed by
`mmap`/`munmap` (POSIX) or `VirtualAlloc`/`VirtualFree` (`#ifdef
_WIN32`), instead of relying on the platform malloc's own
large-allocation heuristics. The Windows branch was written to be
correct by inspection (mirroring the POSIX branch's logic) but has
never actually been exercised on real Windows CI — `eval/README.md`'s
Windows section still documents `iterator-invalidation` as a
non-reproduction from *before* this fix existed.

### Proposal

1. Check (or trigger) the next `windows-eval` CI run and see whether
   `iterator-invalidation` now reproduces.
2. If it does: update `eval/ground_truth.py`'s `iterator-invalidation`
   entry notes and `eval/README.md`'s Windows section to reflect the
   fix, same as was done for the macOS fix.
3. If it doesn't: capture the actual CI output (`eval/run_eval.py`'s
   "Signal/frame/source mismatches" report section, or the
   non-reproduction diagnostic, will have it) and debug the
   `DirectMapAllocator`'s `_WIN32` branch specifically —
   `VirtualFree(p, n * sizeof(T), MEM_DECOMMIT)` on deallocate, matching
   the POSIX branch's `PROT_NONE` remap strategy so the freed address
   can't be silently reused by a later allocation.

### Where to look

- `examples/common/iterator-invalidation.cpp` — the `DirectMapAllocator`
  class and its `#ifdef _WIN32` branches
- `eval/ground_truth.py`'s `iterator-invalidation` entry — current notes
  describe the pre-fix state
- `eval/README.md`'s macOS section — describes the exact same fix
  already confirmed working there, as a reference for what "confirmed"
  documentation should look like

### Why this is a good first issue

Might not even need new code — could just be confirming a fix already
works and updating two doc files. If it does need a fix, the POSIX
branch is a complete, working reference implementation to mirror.

---

## 4. Windows eval: capture `__fastfail` crashes by running examples under `cdb.exe`

**Label:** `help wanted`

### Problem

Five Windows eval examples never reproduce: `double-free`,
`heap-corruption`, `concurrent-vector-race` (exit `0xC0000374`,
`STATUS_HEAP_CORRUPTION`) and `exception-in-destructor-terminate` /
`lock-order-inversion-deadlock` (both `abort()`-based, exit
`0xC0000409`). All five never reach `crashdump.h`'s
`SetUnhandledExceptionFilter` handler at all.

Root cause (already diagnosed, see `eval/README.md`'s Windows section):
these are Windows `__fastfail` codes. By design, `__fastfail` bypasses
normal SEH dispatch unless a debugger is already attached — specifically
so a corrupted-heap process can't have its termination hijacked by its
own (possibly also corrupted) handler. A handler registered in the
target process itself structurally cannot catch these.

### Proposal

Run each example under `cdb.exe` itself during crash capture (already
installed in the `windows-eval` CI job for the later analysis step,
just unused during crash capture) rather than relying on the target's
own in-process handler — a debugger *is* notified of `__fastfail`
exceptions even when a standalone process isn't. This is the same shift
already made for macOS (`lldb --batch ... --one-line-on-crash`) to solve
an analogous problem there.

### Where to look

- `eval/run_eval.py`'s `_run_until_crash_windows` — the current
  subprocess-based crash capture to replace/extend
- `eval/run_eval.py`'s `_run_until_crash_macos` — the lldb-wrapped
  pattern to mirror for Windows/cdb.exe
- `examples/windows/cdb_triage_demo.py` — existing cdb.exe invocation
  patterns in this repo to reuse

### Why this is `help wanted` rather than `good first issue`

Bigger lift than the others: needs a working cdb.exe command-line
invocation that runs a target, catches the fault, and writes a dump —
analogous to `process save-core` on macOS but with different syntax and
failure modes, and needs verifying against a real Windows machine or CI
runner to get right.
