#!/usr/bin/env python3
"""TriagePilot crash-triage evaluation harness.

Builds every example under ``examples/common/``, crashes it, generates a
core dump, runs it through TriagePilot's real debugger-backend code path
(the same functions ``analyze_dump`` uses: ``create_session``,
``get_crash_info``, ``run_crash_analysis``, ``locate_faulting_source``), and
scores the result against ``eval/ground_truth.py``.

This measures the deterministic, non-LLM grounding layer: did TriagePilot
correctly identify the crash signal, land on a relevant user-code frame, and
recover the right source file? It intentionally does not grade the
LangGraph LLM root-cause prose -- that needs a model judge and an API key,
and is a separate (harder) axis for a future iteration.

Usage:
    uv run python eval/run_eval.py
    uv run python eval/run_eval.py --keep-cores --output eval/results.md

Requires a real debugger on PATH (gdb on Linux, lldb on macOS, cdb on
Windows) and a compiler (g++/clang++, or cl.exe from a Developer Command
Prompt on Windows). See eval/README.md for platform notes (core dump
generation on Linux may require adjusting /proc/sys/kernel/core_pattern;
see docs/TROUBLESHOOTING.md; macOS/Windows crash capture works differently
from the POSIX-core path -- see eval/README.md's "macOS and Windows crash
capture" section).
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import NamedTuple

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(EVAL_DIR)
COMMON_DIR = os.path.join(REPO_ROOT, "examples", "common")
SRC_DIR = os.path.join(REPO_ROOT, "src")

# Allow running straight out of a checkout without `pip install -e .` first.
sys.path.insert(0, SRC_DIR)

from ground_truth import GROUND_TRUTH, CrashGroundTruth  # noqa: E402

from triagepilot.backends import create_session, detect_debugger_type  # noqa: E402
from triagepilot.memory.signature import extract_crash_signature  # noqa: E402
from triagepilot.tools.debugger_tools import locate_faulting_source  # noqa: E402

SIGNAL_PATTERN = re.compile(r"\b(SIG[A-Z]+)\b")

# Examples that structurally cannot reproduce their crash on a given
# platform -- not flaky, just built around a mechanism that platform's
# toolchain/allocator doesn't have. (platform_label, excluded_names, reason)
_PLATFORM_EXCLUSIONS: dict[str, tuple[str, set[str], str]] = {
    "win32": (
        "Windows",
        {"heap-metadata-corruption"},
        "its chunk-header-stomp technique calls glibc's malloc_usable_size() and "
        "depends on the tracking record landing immediately after the packet "
        "buffer on that heap (see the HMC_GLIBC_TECHNIQUE guard in "
        "heap-metadata-corruption.cpp, which is off outside Linux+glibc) -- "
        "confirmed live on real Windows 11 (real cl.exe, real cdb.exe) that "
        "without it the example just exits 0 every attempt: the underlying "
        "off-by-one is real, but the MSVC CRT heap's chunk layout isn't "
        "glibc's, and reverse-engineering a Windows-specific equivalent (NT "
        "Heap/LFH chunk headers are cookie-obfuscated since Windows 8, unlike "
        "glibc's plain-text header) is the same class of fragile, "
        "version-specific problem that got this example excluded on macOS "
        "below rather than attempted (see eval/README.md's Windows section)",
    ),
    "darwin": (
        "macOS",
        {"heap-metadata-corruption"},
        "its chunk-header-stomp technique calls glibc's malloc_usable_size() "
        "and depends on conn_record landing immediately after buf in memory -- "
        "confirmed on real macOS hardware that libmalloc doesn't even allocate "
        "them in that relative order, so the technique's premise doesn't hold "
        "there (see eval/README.md's macOS section)",
    ),
}


@dataclass
class ExampleResult:
    entry: CrashGroundTruth
    reproduced: bool = False
    attempts: int = 0
    signal_seen: str | None = None
    signal_match: bool = False
    frame_match: bool = False
    source_match: bool = False
    error: str | None = None
    matched_function: str | None = None
    diagnostic: str | None = None
    # Raw crash_info+analysis text, kept only when a reproduced crash scored
    # under 100% -- otherwise a signal/frame/source miss is a dead end to
    # debug: was it a debugger-output-format change on this platform/OS
    # version (see eval/README.md's ESR_EC/abort-backtrace findings), a
    # genuinely wrong result, or something else? Bounded the same way
    # non-reproduction diagnostics are.
    mismatch_diagnostic: str | None = None

    @property
    def score(self) -> float | None:
        if not self.reproduced:
            return None
        return sum([self.signal_match, self.frame_match, self.source_match]) / 3.0


def _try_raise_core_ulimit() -> None:
    """Best-effort: raise RLIMIT_CORE for this process and its children."""
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
        resource.setrlimit(resource.RLIMIT_CORE, (resource.RLIM_INFINITY, hard))
    except (ImportError, ValueError, OSError):
        pass  # Not available (Windows) or not permitted (sandboxed CI) -- ignore.


def _compiler() -> str:
    for candidate in (os.environ.get("CXX"), "g++", "clang++"):
        if candidate and shutil.which(candidate):
            return candidate
    raise RuntimeError("No C++ compiler found (looked for $CXX, g++, clang++)")


def build_binary(entry: CrashGroundTruth, out_dir: str) -> str:
    """Compile one example with debug info; returns the executable path."""
    if sys.platform == "win32":
        return _build_binary_windows(entry, out_dir)

    cxx = _compiler()
    src = os.path.join(COMMON_DIR, entry.source_file)
    out = os.path.join(out_dir, entry.name)
    cmd = [cxx, "-g", "-O0", "-std=c++17", f"-I{COMMON_DIR}", "-o", out, src]
    if entry.needs_pthread:
        cmd.append("-lpthread")
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return out


def _build_binary_windows(entry: CrashGroundTruth, out_dir: str) -> str:
    """Compile with cl.exe, matching examples/windows/build.ps1's flags.

    Deliberately does not fall back to a MinGW g++/clang++ that might be on
    PATH: this needs to exercise the same MSVC-compiled binary shape that
    the windows-cdb-smoke-test CI job validates, since MinGW would sidestep
    MSVC-specific issues that job already caught once (C++20 designated
    initializers, which GCC/Clang tolerate as an extension but MSVC rejects
    without /std:c++20 -- see heap-metadata-corruption.cpp's history).
    entry.needs_pthread is a no-op here: the three portable-std::thread
    examples that flag it need an explicit -lpthread on Linux/macOS, but
    nothing extra under MSVC.
    """
    if not shutil.which("cl.exe"):
        raise RuntimeError(
            "cl.exe not found on PATH -- run from a Developer Command Prompt "
            "(or a CI step after ilammy/msvc-dev-cmd)"
        )
    src = os.path.join(COMMON_DIR, entry.source_file)
    out = os.path.join(out_dir, f"{entry.name}.exe")
    pdb = os.path.join(out_dir, f"{entry.name}.pdb")
    obj = os.path.join(out_dir, f"{entry.name}.obj")
    cmd = [
        "cl.exe",
        "/nologo",
        "/Zi",
        "/Od",
        "/MT",
        "/EHsc",
        "/GS-",
        "/std:c++17",
        f"/I{COMMON_DIR}",
        f"/Fo:{obj}",
        f"/Fe:{out}",
        src,
        "/link",
        "/DEBUG",
        "/INCREMENTAL:NO",
        f"/PDB:{pdb}",
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return out


class CrashResult(NamedTuple):
    reproduced: bool
    signal_name: str | None
    dump_path: str | None
    # Raw process output (stdout+stderr merged) from the *last* attempt,
    # kept only when reproduction fails, so a "not reproduced" result is
    # something to look at instead of a dead end -- see the Notes section
    # of the rendered report.
    diagnostic: str | None = None


# Number of trailing characters of process output kept in `diagnostic`.
# crashdump.h's own print happens right before the process exits, so the
# tail is what matters; this just bounds how much a chatty example can
# bloat the report.
_DIAGNOSTIC_TAIL_CHARS = 1000


def _tail(text: str | None) -> str | None:
    if not text:
        return text
    return text if len(text) <= _DIAGNOSTIC_TAIL_CHARS else "..." + text[-_DIAGNOSTIC_TAIL_CHARS:]


# Number of leading characters of crash_info+analysis kept for a
# signal/frame/source mismatch diagnostic. Unlike _tail() above, the
# interesting part here (stop-reason line, backtrace) is at the *start* of
# the debugger's own output -- the verbose "Registers" section is last and
# is what a length bound should cut off, not the part that actually matters.
_MISMATCH_HEAD_CHARS = 3000


def _head(text: str | None) -> str | None:
    if not text:
        return text
    return text if len(text) <= _MISMATCH_HEAD_CHARS else text[:_MISMATCH_HEAD_CHARS] + "..."


def run_until_crash(binary: str, run_dir: str, max_attempts: int) -> CrashResult:
    """Dispatch to the platform-appropriate crash-and-capture strategy.

    Each example self-installs a crash handler via crashdump.h, but *how*
    that handler's output turns into a file this harness can hand to
    create_session() differs per platform -- see each helper's docstring.
    """
    if sys.platform == "win32":
        return _run_until_crash_windows(binary, run_dir, max_attempts)
    if sys.platform == "darwin":
        return _run_until_crash_macos(binary, run_dir, max_attempts)
    return _run_until_crash_linux(binary, run_dir, max_attempts)


def _run_until_crash_linux(binary: str, run_dir: str, max_attempts: int) -> CrashResult:
    """Run ``binary`` up to ``max_attempts`` times until a core file appears.

    crashdump.h's POSIX branch re-raises the signal with default disposition
    after printing "[crashdump] Caught signal ..." to stderr, so the OS
    itself writes the core file (see eval/README.md for the
    /proc/sys/kernel/core_pattern prerequisite).
    """
    last_output = None
    for _attempt in range(max_attempts):
        before = set(glob.glob(os.path.join(run_dir, "core*")))
        proc = subprocess.run(
            [binary],
            cwd=run_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )
        last_output = proc.stdout
        after = set(glob.glob(os.path.join(run_dir, "core*")))
        new_cores = after - before
        signal_name = None
        if proc.returncode < 0:
            import signal as signal_module

            try:
                signal_name = signal_module.Signals(-proc.returncode).name
            except ValueError:
                signal_name = f"SIG{-proc.returncode}"
        elif "Caught signal" in (proc.stdout or ""):
            m = SIGNAL_PATTERN.search(proc.stdout)
            if m:
                signal_name = m.group(1)

        if new_cores:
            return CrashResult(True, signal_name, sorted(new_cores)[0])

    return CrashResult(False, None, None, diagnostic=_tail(last_output))


def _run_until_crash_macos(binary: str, run_dir: str, max_attempts: int) -> CrashResult:
    """Run ``binary`` under lldb so it intercepts the crash before macOS's
    ReportCrash agent does, saving a core via ``process save-core``.

    Same technique as examples/macos/gen_core_mac.sh (see its comments for
    why a plain `ulimit -c unlimited` + direct subprocess.run doesn't
    reliably produce a core on modern macOS) -- ``--one-line-on-crash``
    commands only run when lldb's stop reason is a crash, so a clean exit
    (crash didn't reproduce this attempt) just falls through to the next
    attempt with no core file written.
    """
    lldb = shutil.which("lldb")
    if not lldb:
        raise RuntimeError("lldb not found on PATH")

    last_output = None
    for attempt in range(max_attempts):
        core_path = os.path.join(run_dir, f"core.{os.path.basename(binary)}.{attempt}")
        proc = subprocess.run(
            [
                lldb,
                binary,
                "--batch",
                "-o",
                "run",
                "--one-line-on-crash",
                f"process save-core {core_path}",
                "--one-line-on-crash",
                "quit",
            ],
            cwd=run_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )
        last_output = proc.stdout

        # Signal name comes from crashdump.h's own "[crashdump] Caught
        # signal ..." print (which lldb passes through as the target's
        # inherited stdout/stderr), not from lldb's own stop-reason text --
        # keeps this consistent with how the Linux branch detects it.
        signal_name = None
        if "Caught signal" in (proc.stdout or ""):
            m = SIGNAL_PATTERN.search(proc.stdout)
            if m:
                signal_name = m.group(1)

        if os.path.isfile(core_path):
            return CrashResult(True, signal_name, core_path)

    return CrashResult(False, None, None, diagnostic=_tail(last_output))


# NTSTATUS codes cdb's stop-reason line can report, mapped to the nearest
# POSIX-equivalent signal so unmodified ground_truth.py entries (written in
# terms of SIGSEGV/SIGABRT) score correctly. STATUS_HEAP_CORRUPTION and
# STATUS_STACK_BUFFER_OVERRUN are both __fastfail codes (see this module's
# docstring above and eval/README.md's Windows section) -- Windows' OS-level
# equivalent of the glibc/libmalloc abort() chain double-free.cpp,
# heap-corruption.cpp, etc. trigger on Linux/macOS. Confirmed live (real
# Windows 11, real cdb 10.0.29617.1000): use-after-free reports c0000005,
# double-free reports c0000374.
_WINDOWS_NTSTATUS_TO_SIGNAL = {
    "c0000005": "SIGSEGV",  # STATUS_ACCESS_VIOLATION
    "c00000fd": "SIGSEGV",  # STATUS_STACK_OVERFLOW
    "c0000374": "SIGABRT",  # STATUS_HEAP_CORRUPTION (__fastfail)
    "c0000409": "SIGABRT",  # STATUS_STACK_BUFFER_OVERRUN (__fastfail)
    # STATUS_BREAKPOINT. Ambiguous in general (an intentional __debugbreak()
    # would report the same code), but none of these examples call that --
    # confirmed live that concurrent-vector-race.cpp's genuine data race
    # (four threads, one unlocked std::vector) can manifest as *either* a
    # plain access violation *or* this, with !analyze -v's own
    # Failure.Bucket reading "HEAP_CORRUPTION_ACTIONABLE_..._DOUBLE_FREE_
    # 80000003_..." on the runs that hit it -- Windows' page-heap/heap-
    # corruption-detection breaking in via a breakpoint exception rather
    # than a __fastfail code, for what's fundamentally the same class of
    # bug double-free.cpp/heap-corruption.cpp trigger.
    "80000003": "SIGABRT",  # STATUS_BREAKPOINT (heap-corruption-detection break-in)
}


def _run_until_crash_windows(binary: str, run_dir: str, max_attempts: int) -> CrashResult:
    """Run ``binary`` under cdb.exe up to ``max_attempts`` times until it writes a dump.

    Earlier versions of this function ran the binary directly and watched
    <exe-dir>\\dumps\\ for the .dmp crashdump.h's own SetUnhandledExceptionFilter
    handler writes. That catches a plain STATUS_ACCESS_VIOLATION fine, but
    `double-free`, `heap-corruption`, `concurrent-vector-race`
    (STATUS_HEAP_CORRUPTION) and the abort()-based
    `exception-in-destructor-terminate` / `lock-order-inversion-deadlock`
    (STATUS_STACK_BUFFER_OVERRUN) never reached it at all: both are Windows
    `__fastfail` codes, which by design bypass normal SEH dispatch --
    including a handler registered in the target process itself -- unless a
    debugger is already attached, specifically so a corrupted-heap process
    can't have its termination hijacked by its own (possibly also corrupted)
    handler.

    Running the target under `cdb.exe` itself sidesteps this the same way
    `_run_until_crash_macos` runs under lldb instead of trusting a
    self-installed handler: a debugger *is* notified of `__fastfail`
    exceptions first-chance, even when a standalone process wouldn't be, so
    cdb can write the dump itself and never has to reach crashdump.h's
    handler at all. `-hd` keeps the process heap's debug-mode-under-a-debugger
    behavior off, so heap-corruption detection timing matches an undebugged
    run as closely as possible (see "Why does my program run differently
    under the debugger?" for the NTDLL-level debug heap this avoids).
    """
    cdb = _find_cdb()
    if not cdb:
        raise RuntimeError(
            "cdb.exe not found -- run from a Developer Command Prompt with the "
            "Windows SDK's Debugging Tools installed, or pass its directory on PATH"
        )

    last_output = None
    for attempt in range(max_attempts):
        dump_path = os.path.join(run_dir, f"{os.path.basename(binary)}.{attempt}.dmp")
        proc = subprocess.run(
            [cdb, "-g", "-G", "-hd", "-c", f'g;.dump /ma "{dump_path}";q', binary],
            cwd=run_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=45,
        )
        last_output = proc.stdout

        # cdb's own stop-reason line (e.g. "... - code c0000374 (first
        # chance)") is the closest analogue to crashdump.h's "Caught signal
        # ..." print on the other platforms. Deliberately take the *last*
        # "code XXXXXXXX" match, not the first: cdb's own startup chatter can
        # print an earlier, unrelated code (observed: STATUS_BREAKPOINT
        # 0x80000003 from the loader breakpoint cdb's -g flag auto-continues
        # past) before the actual crash. This can't be the sole/authoritative
        # signal check -- extract_crash_signature()'s CDB path returns
        # Windows-native labels (e.g. "ACCESS_VIOLATION" from BUGCHECK_STR),
        # not POSIX names, by design (see test_memory.py's
        # test_extract_cdb_signature), so it never matches ground_truth.py's
        # SIGSEGV/SIGABRT entries for this backend -- this NTSTATUS mapping
        # is what actually makes signal_match succeed on Windows.
        signal_name = None
        code_matches = re.findall(r"-\s*code\s+([0-9a-fA-F]+)\b", proc.stdout or "")
        if code_matches:
            code = code_matches[-1].lower()
            signal_name = _WINDOWS_NTSTATUS_TO_SIGNAL.get(code, f"EXIT_0x{code.upper()}")

        if os.path.isfile(dump_path):
            return CrashResult(True, signal_name, dump_path)

    return CrashResult(False, None, None, diagnostic=_tail(last_output))


def _find_cdb() -> str | None:
    """Locate cdb.exe using the same search order the real backend uses.

    Deliberately not just ``shutil.which("cdb")``: cdb.exe is usually
    installed under the Windows SDK's Debugging Tools directory, which
    doesn't put itself on PATH, and CDBSession.find_debugger_executable()
    already knows those default install locations (see backends/cdb.py's
    DEFAULT_CDB_PATHS) -- reusing it keeps this harness consistent with
    what create_session(debugger_type="cdb") will find later for analysis.
    """
    from triagepilot.backends.cdb import CDBSession

    return CDBSession.find_debugger_executable()


def evaluate_example(
    entry: CrashGroundTruth,
    build_dir: str,
    debugger_type: str,
    max_attempts: int,
    keep_cores: bool,
) -> ExampleResult:
    result = ExampleResult(entry=entry)

    try:
        binary = build_binary(entry, build_dir)
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        result.error = f"build failed: {exc}"
        return result

    run_dir = tempfile.mkdtemp(prefix=f"triagepilot-eval-{entry.name}-")
    try:
        crash = run_until_crash(binary, run_dir, max_attempts)
        result.reproduced = crash.reproduced
        result.signal_seen = crash.signal_name
        signal_name, core_path = crash.signal_name, crash.dump_path

        if not crash.reproduced:
            result.error = "crash did not reproduce (no core/dump file after retries)"
            result.diagnostic = crash.diagnostic
            return result

        session = create_session(
            dump_path=core_path,
            image_path=binary,
            debugger_type=debugger_type,
            # cyclic-refcount-stack-overflow's CDB session hit "CDB
            # initialization timed out" at 30s on Windows in CI -- its
            # MiniDumpWithFullMemory dump plus CDB's symbol loading for a
            # deeply-templated shared_ptr/STL recursion apparently needs
            # more than that on a loaded runner, even though gdb/lldb
            # handle the equivalent core/dump for the same example in well
            # under 30s. A generous ceiling only costs time on the (rare)
            # examples that actually need it; it can't make a
            # faster-opening dump slower. Also doubles as run_crash_analysis()'s
            # default per-command timeout (e.g. for `lm`) -- confirmed live
            # that "!analyze -v" alone can take ~159s (mostly a first-use
            # online WER bucket-ID lookup), so this needs real headroom above
            # that, not just above session-open time.
            timeout=200,
        )
        try:
            crash_info = session.get_crash_info()
            analysis = session.run_crash_analysis()
        finally:
            session.shutdown()

        combined = f"{crash_info}\n{analysis}"

        # 1. Signal match. Prefer TriagePilot's own signal-extraction path
        # (extract_crash_signature -- the same one auto_save_analysis and
        # the LangGraph memory nodes use) over a raw substring search: this
        # is what the eval is actually meant to be grading ("did TriagePilot
        # correctly identify the crash signal"), and a raw substring check
        # gives a false pass/fail whenever the debugger's own wording
        # doesn't happen to contain the literal signal name -- e.g. lldb on
        # Apple Silicon reports arm64 exception classes like
        # "ESR_EC_DABORT_EL0" instead of "SIGSEGV" in its stop-reason text,
        # which extract_crash_signature translates but a substring search
        # never would.
        detected_signal = extract_crash_signature(combined, debugger_type).exception_type
        result.signal_match = (
            any(sig in combined for sig in entry.expected_signals)
            or (signal_name is not None and signal_name in entry.expected_signals)
            or (detected_signal is not None and detected_signal in entry.expected_signals)
        )

        # 2. Frame / function match
        for fn in entry.expected_functions:
            if fn in combined:
                result.frame_match = True
                result.matched_function = fn
                break

        # 3. Source localization match
        source_section = locate_faulting_source(analysis, repo_path=REPO_ROOT)
        if source_section and entry.expected_file in source_section:
            result.source_match = True

        if not (result.signal_match and result.frame_match and result.source_match):
            result.mismatch_diagnostic = _head(combined)

        return result
    except Exception as exc:  # noqa: BLE001 -- report, don't crash the whole eval run
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        if not keep_cores:
            shutil.rmtree(run_dir, ignore_errors=True)


def render_results_md(results: list[ExampleResult], debugger_type: str) -> str:
    reproduced = [r for r in results if r.reproduced]
    scored = [r for r in reproduced if r.score is not None]
    overall = sum(r.score for r in scored) / len(scored) if scored else 0.0

    lines = [
        "# TriagePilot Crash-Triage Eval Results",
        "",
        f"Backend: `{debugger_type}` | Examples: {len(results)} | "
        f"Reproduced: {len(reproduced)}/{len(results)} | "
        f"Aggregate accuracy (reproduced only): **{overall * 100:.0f}%**",
        "",
        "| Example | Category | Reproduced | Signal | Frame Found | Source Located | Score |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if not r.reproduced:
            lines.append(
                f"| `{r.entry.name}` | {r.entry.category} | no | - | - | - | "
                f"_not reproduced{' (known flaky)' if r.entry.known_flaky else ''}_ |"
            )
            continue
        lines.append(
            f"| `{r.entry.name}` | {r.entry.category} | yes | "
            f"{'OK' if r.signal_match else 'MISS'} ({r.signal_seen or '?'}) | "
            f"{'OK' if r.frame_match else 'MISS'}{f' (`{r.matched_function}`)' if r.matched_function else ''} | "
            f"{'OK' if r.source_match else 'MISS'} | {r.score * 100:.0f}% |"
        )

    errors = [r for r in results if r.error]
    if errors:
        lines += ["", "## Notes", ""]
        for r in errors:
            lines.append(f"- `{r.entry.name}`: {r.error}")
            if r.diagnostic:
                # <details> instead of embedding raw newlines in the bullet
                # above -- a multi-line string breaks CommonMark list-item
                # continuation without careful indentation, this doesn't.
                lines += [
                    "  <details><summary>last run's output</summary>",
                    "",
                    "  ```",
                    *(f"  {line}" for line in r.diagnostic.splitlines()),
                    "  ```",
                    "  </details>",
                    "",
                ]

    mismatches = [r for r in reproduced if r.mismatch_diagnostic]
    if mismatches:
        lines += ["", "## Signal/frame/source mismatches", ""]
        for r in mismatches:
            missed = [
                name
                for name, ok in (
                    ("signal", r.signal_match),
                    ("frame", r.frame_match),
                    ("source", r.source_match),
                )
                if not ok
            ]
            lines.append(f"- `{r.entry.name}`: missed {', '.join(missed)}")
            lines += [
                "  <details><summary>crash_info + analysis</summary>",
                "",
                "  ```",
                *(f"  {line}" for line in r.mismatch_diagnostic.splitlines()),
                "  ```",
                "  </details>",
                "",
            ]

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--debugger-type",
        default="auto",
        choices=["auto", "gdb", "lldb", "cdb"],
        help="Debugger backend to use (default: auto-detect for this platform)",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=5,
        help="Retries per example before giving up on reproducing the crash",
    )
    parser.add_argument("--keep-cores", action="store_true", help="Do not delete run directories")
    parser.add_argument(
        "--output",
        default=os.path.join(EVAL_DIR, "results.md"),
        help="Where to write the results table (default: eval/results.md)",
    )
    parser.add_argument(
        "--only",
        nargs="*",
        default=None,
        help="Only run the named example(s), e.g. --only use-after-free double-free",
    )
    args = parser.parse_args()

    debugger_type = args.debugger_type
    if debugger_type == "auto":
        debugger_type = detect_debugger_type()

    _try_raise_core_ulimit()

    entries = GROUND_TRUTH
    platform_exclusion = _PLATFORM_EXCLUSIONS.get(sys.platform)
    if platform_exclusion:
        platform_label, excluded, reason = platform_exclusion
        skipped = [e.name for e in entries if e.name in excluded]
        entries = [e for e in entries if e.name not in excluded]
        for name in skipped:
            print(f"[eval] skipping {name} on {platform_label} ({reason})", file=sys.stderr)
    if args.only:
        entries = [e for e in entries if e.name in args.only]

    build_dir = tempfile.mkdtemp(prefix="triagepilot-eval-build-")
    results: list[ExampleResult] = []
    try:
        for entry in entries:
            print(f"[eval] {entry.name} ...", file=sys.stderr)
            result = evaluate_example(
                entry, build_dir, debugger_type, args.max_attempts, args.keep_cores
            )
            results.append(result)
            status = "reproduced" if result.reproduced else "NOT REPRODUCED"
            print(f"[eval]   -> {status}  score={result.score}", file=sys.stderr)
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)

    report = render_results_md(results, debugger_type)
    print(report)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"[eval] wrote {args.output}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
