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

Requires a real debugger on PATH (gdb on Linux, lldb on macOS) and a
compiler (g++/clang++). See eval/README.md for platform notes (core dump
generation on Linux may require adjusting /proc/sys/kernel/core_pattern;
see docs/TROUBLESHOOTING.md).
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

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(EVAL_DIR)
COMMON_DIR = os.path.join(REPO_ROOT, "examples", "common")
SRC_DIR = os.path.join(REPO_ROOT, "src")

# Allow running straight out of a checkout without `pip install -e .` first.
sys.path.insert(0, SRC_DIR)

from ground_truth import GROUND_TRUTH, CrashGroundTruth  # noqa: E402

from triagepilot.backends import create_session, detect_debugger_type  # noqa: E402
from triagepilot.tools.debugger_tools import locate_faulting_source  # noqa: E402

SIGNAL_PATTERN = re.compile(r"\b(SIG[A-Z]+)\b")


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
    cxx = _compiler()
    src = os.path.join(COMMON_DIR, entry.source_file)
    out = os.path.join(out_dir, entry.name)
    cmd = [cxx, "-g", "-O0", "-std=c++17", f"-I{COMMON_DIR}", "-o", out, src]
    if entry.needs_pthread:
        cmd.append("-lpthread")
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return out


def run_until_crash(binary: str, run_dir: str, max_attempts: int) -> tuple[bool, str | None, str | None]:
    """Run ``binary`` up to ``max_attempts`` times until a core file appears.

    Returns (reproduced, signal_name, core_path).
    """
    for _attempt in range(max_attempts):
        before = set(glob.glob(os.path.join(run_dir, "core*")))
        proc = subprocess.run(
            [binary],
            cwd=run_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        after = set(glob.glob(os.path.join(run_dir, "core*")))
        new_cores = after - before
        signal_name = None
        if proc.returncode < 0:
            import signal as signal_module

            try:
                signal_name = signal_module.Signals(-proc.returncode).name
            except ValueError:
                signal_name = f"SIG{-proc.returncode}"
        elif "Caught signal" in (proc.stderr or ""):
            m = SIGNAL_PATTERN.search(proc.stderr)
            if m:
                signal_name = m.group(1)

        if new_cores:
            return True, signal_name, sorted(new_cores)[0]

    return False, None, None


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
        reproduced, signal_name, core_path = run_until_crash(binary, run_dir, max_attempts)
        result.reproduced = reproduced
        result.signal_seen = signal_name

        if not reproduced:
            result.error = "crash did not reproduce (no core file after retries)"
            return result

        session = create_session(
            dump_path=core_path,
            image_path=binary,
            debugger_type=debugger_type,
            timeout=30,
        )
        try:
            crash_info = session.get_crash_info()
            analysis = session.run_crash_analysis()
        finally:
            session.shutdown()

        combined = f"{crash_info}\n{analysis}"

        # 1. Signal match
        result.signal_match = any(sig in combined for sig in entry.expected_signals) or (
            signal_name is not None and signal_name in entry.expected_signals
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
