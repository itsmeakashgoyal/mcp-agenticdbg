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

Direct testing surfaced three examples from the original set of ten that did
not reproduce their crash in 5/5 attempts on Ubuntu 22.04 **aarch64**, and
one whose signal is architecture-dependent. These are marked
`known_flaky=True` / documented in `ground_truth.py` rather than silently
ignored:

| Example | Finding |
|---|---|
| `heap-corruption` | Did not crash in 5/5 runs on aarch64; the 16-byte overrun didn't corrupt a field this glibc/arch combination validates on free(). Likely more reliable on x86_64. |
| `heap-metadata-corruption` | Same as above -- did not crash in 5/5 runs on aarch64. |
| `thread-uaf` | Timing-dependent race between worker/watchdog threads; the `usleep()`-based window did not line up in 5/5 runs under this sandbox's scheduler. |
| `stack-buffer-overrun` | Crashed reliably, but with `SIGBUS` on aarch64 vs. the `SIGSEGV` implied by the original (Windows-oriented) header comment -- ARM enforces stricter alignment faults on indirect jumps to garbage addresses than x86_64 does. |

The other thirteen examples (including all four new "advanced" ones and all
three new "multithreading" ones added alongside this harness) reproduced
their documented signal in every run tested -- including the three new
multithreading examples, each stress-tested 5-8 times:

| Example | Result |
|---|---|
| `concurrent-vector-race` | Crashed 6/6 runs (SIGSEGV or SIGABRT -- genuine data race, signal varies) |
| `lock-order-inversion-deadlock` | Deadlocked + watchdog-aborted 6/6 runs (fully deterministic by construction, not a race) |
| `detached-thread-dangling-stack` | Crashed 8/8 runs (SIGBUS on aarch64; expect SIGSEGV on x86_64) |

None of the non-reproduction findings reflect a TriagePilot bug -- it's
example-program determinism, worth hardening separately (e.g. forcing
`heap-corruption`'s overrun to cross an mmap threshold the way
`iterator-invalidation.cpp` does, or replacing `thread-uaf`'s `usleep()`
handshake with the same explicit-barrier technique
`lock-order-inversion-deadlock.cpp` and `concurrent-vector-race.cpp` use to
stay deterministic/high-probability instead of sleep-timing-dependent).

## CI

A `.github/workflows/ci.yml` job (`eval`) runs this harness on every push to
`master` using a real `ubuntu-latest` runner (full internet access, so
`gdb` installs cleanly and there's no sandboxed-core-dump restriction). See
that job for the exact setup steps this README assumes.
