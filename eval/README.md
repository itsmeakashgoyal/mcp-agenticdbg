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
`ground_truth.py`:

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

Requires a real debugger on `PATH` (`gdb` on Linux, `lldb` on macOS) and a
C++ compiler (`g++`/`clang++`).

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

## CI

A `.github/workflows/ci.yml` job (`eval`) runs this harness on every push to
`master` using a real `ubuntu-latest` runner (full internet access, so
`gdb` installs cleanly and there's no sandboxed-core-dump restriction). See
that job for the exact setup steps this README assumes.
