"""Ground truth for the TriagePilot crash-triage evaluation harness.

Each entry describes one example program under ``examples/common/`` and the
facts a correct crash analysis must surface:

- ``expected_signals``   -- OS signal(s) considered correct. A list because a
  couple of examples are architecture-sensitive (see notes).
- ``expected_functions`` -- user-code function names, any one of which is an
  acceptable "found the crash site" answer. Several examples crash inside an
  unresolved/library frame (an indirect call through a corrupted vtable, or a
  glibc abort chain), so the nearest meaningful *named* frame is listed
  instead of a single ground-truth symbol.
- ``expected_file``      -- source file the fault maps back to.
- ``known_flaky``        -- True when the crash itself is not guaranteed to
  reproduce on every run/architecture (documented from direct testing, not
  guesswork -- see eval/README.md "Known example reliability" section).

Scope note: this ground truth only covers what TriagePilot's deterministic
debugger-grounding layer (get_crash_info / run_crash_analysis /
locate_faulting_source) can be objectively graded on -- signal, crash
location, and source recovery. It intentionally does NOT grade the LLM
root-cause prose from the LangGraph path, which needs a model judge and an
API key and is a separate, harder-to-grade axis.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CrashGroundTruth:
    name: str
    source_file: str
    category: str  # "simple" | "complex" | "advanced"
    expected_signals: tuple[str, ...]
    expected_functions: tuple[str, ...]
    expected_file: str
    needs_pthread: bool = False
    known_flaky: bool = False
    notes: str = ""


GROUND_TRUTH: list[CrashGroundTruth] = [
    CrashGroundTruth(
        name="stack-overflow",
        source_file="stack-overflow.cpp",
        category="simple",
        expected_signals=("SIGSEGV",),
        expected_functions=("recursive_descent",),
        expected_file="stack-overflow.cpp",
    ),
    CrashGroundTruth(
        name="use-after-free",
        source_file="use-after-free.cpp",
        category="simple",
        expected_signals=("SIGSEGV",),
        expected_functions=("main",),
        expected_file="use-after-free.cpp",
    ),
    CrashGroundTruth(
        name="double-free",
        source_file="double-free.cpp",
        category="simple",
        expected_signals=("SIGABRT",),
        expected_functions=("main",),
        expected_file="double-free.cpp",
        notes="glibc tcache double-free detection; abort chain sits above main.",
    ),
    CrashGroundTruth(
        name="vtable-corruption",
        source_file="vtable-corruption.cpp",
        category="simple",
        expected_signals=("SIGSEGV",),
        expected_functions=("main", "speak"),
        expected_file="vtable-corruption.cpp",
        notes="Crash frame is an unresolved indirect call; nearest named frame is main().",
    ),
    CrashGroundTruth(
        name="stack-buffer-overrun",
        source_file="stack-buffer-overrun.cpp",
        category="simple",
        expected_signals=("SIGSEGV", "SIGBUS"),
        expected_functions=("process_command", "main"),
        expected_file="stack-buffer-overrun.cpp",
        notes=(
            "Signal is architecture-dependent: jumping to an unmapped address is "
            "SIGSEGV on x86_64 but can surface as SIGBUS on aarch64 due to stricter "
            "alignment-fault handling on indirect jumps (observed directly on "
            "Ubuntu 22.04 aarch64)."
        ),
    ),
    CrashGroundTruth(
        name="heap-corruption",
        source_file="heap-corruption.cpp",
        category="simple",
        expected_signals=("SIGABRT",),
        expected_functions=("main",),
        expected_file="heap-corruption.cpp",
        known_flaky=True,
        notes=(
            "Did not reproduce in 5/5 runs on Ubuntu 22.04 aarch64 in direct testing "
            "-- the 16-byte overrun did not corrupt a field glibc's allocator "
            "validates on this arch/allocator-version combination. Likely more "
            "reliable on x86_64 glibc, matching the example's original design intent."
        ),
    ),
    CrashGroundTruth(
        name="deep-callchain-nullptr",
        source_file="deep-callchain-nullptr.cpp",
        category="complex",
        expected_signals=("SIGSEGV",),
        expected_functions=("evaluate_call",),
        expected_file="deep-callchain-nullptr.cpp",
    ),
    CrashGroundTruth(
        name="heap-metadata-corruption",
        source_file="heap-metadata-corruption.cpp",
        category="complex",
        expected_signals=("SIGABRT",),
        expected_functions=("flush_buffer",),
        expected_file="heap-metadata-corruption.cpp",
        known_flaky=True,
        notes="Did not reproduce in 5/5 runs on Ubuntu 22.04 aarch64 in direct testing.",
    ),
    CrashGroundTruth(
        name="multi-inheritance-crash",
        source_file="multi-inheritance-crash.cpp",
        category="complex",
        expected_signals=("SIGSEGV",),
        expected_functions=("run_pipeline", "main"),
        expected_file="multi-inheritance-crash.cpp",
        notes="Crash frame is an unresolved indirect call through the mis-adjusted vtable.",
    ),
    CrashGroundTruth(
        name="thread-uaf",
        source_file="thread-uaf.cpp",
        category="complex",
        expected_signals=("SIGSEGV",),
        expected_functions=("record", "process_request"),
        expected_file="thread-uaf.cpp",
        needs_pthread=True,
        known_flaky=True,
        notes=(
            "Timing-dependent race between the worker and watchdog threads. Did not "
            "reproduce in 5/5 runs on Ubuntu 22.04 aarch64 in direct testing; the "
            "usleep()-based timing window did not line up under this sandbox's "
            "scheduler."
        ),
    ),
    # --- New "advanced" examples ------------------------------------------
    CrashGroundTruth(
        name="format-string-crash",
        source_file="format-string-crash.cpp",
        category="advanced",
        expected_signals=("SIGSEGV",),
        expected_functions=("log_unsafe",),
        expected_file="format-string-crash.cpp",
    ),
    CrashGroundTruth(
        name="iterator-invalidation",
        source_file="iterator-invalidation.cpp",
        category="advanced",
        expected_signals=("SIGSEGV",),
        expected_functions=("finalize_sample",),
        expected_file="iterator-invalidation.cpp",
    ),
    CrashGroundTruth(
        name="exception-in-destructor-terminate",
        source_file="exception-in-destructor-terminate.cpp",
        category="advanced",
        expected_signals=("SIGABRT",),
        expected_functions=("~ScopedTransaction", "process_orders_batch", "commit"),
        expected_file="exception-in-destructor-terminate.cpp",
        notes="Top frames are libstdc++ terminate-handler internals, not user code.",
    ),
    CrashGroundTruth(
        name="cyclic-refcount-stack-overflow",
        source_file="cyclic-refcount-stack-overflow.cpp",
        category="advanced",
        expected_signals=("SIGSEGV",),
        expected_functions=("~SceneNode",),
        expected_file="cyclic-refcount-stack-overflow.cpp",
    ),
    # --- New "multithreading" examples -------------------------------------
    CrashGroundTruth(
        name="concurrent-vector-race",
        source_file="concurrent-vector-race.cpp",
        category="multithreading",
        expected_signals=("SIGSEGV", "SIGABRT"),
        expected_functions=("record", "push_back"),
        expected_file="concurrent-vector-race.cpp",
        needs_pthread=True,
        notes=(
            "Genuine data race between four threads on an unlocked std::vector; "
            "the exact signal (SIGSEGV vs. glibc-detected SIGABRT) varies run to "
            "run, but crashed in 6/6 direct test runs on Ubuntu 22.04 aarch64."
        ),
    ),
    CrashGroundTruth(
        name="lock-order-inversion-deadlock",
        source_file="lock-order-inversion-deadlock.cpp",
        category="multithreading",
        expected_signals=("SIGABRT",),
        expected_functions=("transfer_funds",),
        expected_file="lock-order-inversion-deadlock.cpp",
        needs_pthread=True,
        notes=(
            "Deterministic deadlock (not a race) -- a watchdog thread aborts the "
            "process after a fixed timeout so the hang is analyzable. No frame "
            "actually faults; both worker threads are blocked in "
            "pthread_mutex_lock inside transfer_funds() when the dump is taken."
        ),
    ),
    CrashGroundTruth(
        name="detached-thread-dangling-stack",
        source_file="detached-thread-dangling-stack.cpp",
        category="multithreading",
        expected_signals=("SIGSEGV", "SIGBUS"),
        expected_functions=("use_stack_slot", "process_next_request"),
        expected_file="detached-thread-dangling-stack.cpp",
        needs_pthread=True,
        notes=(
            "Signal is architecture-dependent, same as stack-buffer-overrun: "
            "SIGSEGV on x86_64, observed as SIGBUS on aarch64 (6/6 direct test "
            "runs on Ubuntu 22.04 aarch64)."
        ),
    ),
]


def by_name(name: str) -> CrashGroundTruth:
    for entry in GROUND_TRUTH:
        if entry.name == name:
            return entry
    raise KeyError(f"No ground truth entry named {name!r}")
