"""Debugger tool handlers for the MCP server (platform-agnostic)."""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import sys
import time
import threading
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, TextContent, INVALID_PARAMS, INTERNAL_ERROR

from ..backends import (
    DebuggerError,
    DebuggerSession,
    create_session,
    detect_debugger_type,
    get_local_dumps_path as _backend_get_local_dumps_path,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Security: command blocklist & rate limiting
# ---------------------------------------------------------------------------

BLOCKED_COMMAND_PREFIXES_CDB = (
    ".shell", ".create", ".write", ".crash", ".reboot", ".kill", "!bpset",
    ".load", ".unload",        # arbitrary DLL loading
    ".logopen", ".logclose",   # file system writes
    ".writemem",               # write memory to file
    ".dump",                   # create dump file
)
BLOCKED_COMMAND_PREFIXES_GDB = (
    "shell", "!",
)
BLOCKED_COMMAND_PREFIXES_LLDB = (
    "platform shell", "process launch",
)


def validate_debugger_command(command: str, debugger_type: str = "auto") -> None:
    """Reject debugger commands on the security blocklist."""
    normalized = command.strip().lower()
    if debugger_type == "auto":
        debugger_type = detect_debugger_type()

    if debugger_type == "cdb":
        blocklist = BLOCKED_COMMAND_PREFIXES_CDB
    elif debugger_type == "gdb":
        blocklist = BLOCKED_COMMAND_PREFIXES_GDB
    elif debugger_type == "lldb":
        blocklist = BLOCKED_COMMAND_PREFIXES_LLDB
    else:
        blocklist = BLOCKED_COMMAND_PREFIXES_CDB

    for prefix in blocklist:
        if normalized.startswith(prefix):
            logger.warning("Blocked debugger command attempt: %s", command)
            raise McpError(
                ErrorData(
                    code=INVALID_PARAMS,
                    message=(
                        f"Command '{prefix}' is blocked for security reasons. "
                        "Contact an administrator if you need this capability."
                    ),
                )
            )


# Keep backward compat alias
validate_cdb_command = validate_debugger_command


class _TokenBucket:
    """Simple thread-safe token-bucket rate limiter."""

    def __init__(self, rate: float = 10.0, capacity: float = 20.0) -> None:
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def consume(self) -> bool:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


_cmd_rate_limiter = _TokenBucket(rate=10.0, capacity=20.0)

active_sessions: OrderedDict[str, DebuggerSession] = OrderedDict()
_max_concurrent_sessions: int = 5
# Protects active_sessions against concurrent create/evict races
_session_lock = threading.Lock()


def set_max_concurrent_sessions(limit: int) -> None:
    global _max_concurrent_sessions
    _max_concurrent_sessions = max(1, limit)


def active_session_count() -> int:
    return sum(1 for s in active_sessions.values() if s is not None)


# ---------------------------------------------------------------------------
# Dump-path helpers (platform-aware)
# ---------------------------------------------------------------------------


def get_local_dumps_path(debugger_type: str = "auto") -> Optional[str]:
    """Get the default crash dumps path for the current platform."""
    return _backend_get_local_dumps_path(debugger_type)


def _dump_file_patterns(debugger_type: str = "auto") -> list[str]:
    """Return glob patterns for crash dump files based on platform."""
    if debugger_type == "auto":
        debugger_type = detect_debugger_type()
    if debugger_type == "cdb":
        return ["*.*dmp"]
    elif debugger_type == "lldb" and sys.platform == "darwin":
        return ["*.crash", "*.ips", "core.*", "*.core", "core"]
    else:
        return ["core.*", "*.core", "core"]


# ---------------------------------------------------------------------------
# Faulting source file locator
# ---------------------------------------------------------------------------

_FAULTING_FILE_RE = re.compile(r"^FAULTING_SOURCE_FILE:\s+(.+)$", re.MULTILINE)
_FAULTING_LINE_RE = re.compile(r"^FAULTING_SOURCE_LINE_NUMBER:\s+(\d+)$", re.MULTILINE)
_SOURCE_CONTEXT_LINES = 25
_SOURCE_LOOKUP_MAX_SECONDS = max(5, int(os.environ.get("TRIAGEPILOT_SOURCE_LOOKUP_MAX_SECONDS", "45")))
_SOURCE_LOOKUP_MAX_FILES = max(1000, int(os.environ.get("TRIAGEPILOT_SOURCE_LOOKUP_MAX_FILES", "200000")))
_SOURCE_LOOKUP_SKIP_DIR_NAMES = frozenset(
    {
        ".git", ".hg", ".svn", ".vs", ".idea", ".cache",
        "__pycache__", ".mypy_cache", ".pytest_cache",
        "node_modules",
    }
)

_SOURCE_EXTENSIONS = frozenset((
    ".cpp", ".c", ".cc", ".cxx", ".h", ".hpp", ".hxx", ".inl",
    ".m", ".mm",       # Objective-C / Objective-C++
    ".swift",          # Swift
    ".rs",             # Rust
    ".go",             # Go
))

_SYMBOL_NAME_RE = re.compile(r"^SYMBOL_NAME:\s+(\w+)!([A-Za-z0-9_:~]+)", re.MULTILINE)
_MODULE_NAME_RE = re.compile(r"^MODULE_NAME:\s+(\w+)", re.MULTILINE)
_STACK_FRAME_RE = re.compile(r"(\w+)!([A-Za-z0-9_:~]+)\+0x[0-9a-fA-F]+")

# GDB/LLDB "at file.cpp:42" patterns in backtraces
_GDB_AT_RE = re.compile(
    r"\bat\s+([\w./\-\\]+\.(?:c|cpp|cc|cxx|h|hpp|hxx|inl|rs|go|py|m|mm|swift)):(\d+)",
    re.MULTILINE,
)
# GDB "#N ... in func_name (" frame pattern
_GDB_FRAME_FUNC_RE = re.compile(
    r"^#\d+\s+(?:0x[0-9a-fA-F]+\s+in\s+)?([A-Za-z_][A-Za-z0-9_:<>~*]+)\s*\(",
    re.MULTILINE,
)


def _parse_faulting_source(analysis_text: str) -> Tuple[Optional[str], Optional[int]]:
    """Extract FAULTING_SOURCE_FILE and FAULTING_SOURCE_LINE_NUMBER from analysis output."""
    file_match = _FAULTING_FILE_RE.search(analysis_text)
    line_match = _FAULTING_LINE_RE.search(analysis_text)
    faulting_file = file_match.group(1).strip() if file_match else None
    faulting_line = int(line_match.group(1)) if line_match else None
    return faulting_file, faulting_line


def _new_source_lookup_budget() -> Dict[str, float | int | bool]:
    return {
        "deadline": time.monotonic() + _SOURCE_LOOKUP_MAX_SECONDS,
        "max_files": _SOURCE_LOOKUP_MAX_FILES,
        "files_scanned": 0,
        "stopped": False,
    }


def _is_budget_exhausted(budget: Optional[Dict[str, float | int | bool]]) -> bool:
    if not budget:
        return False
    if bool(budget.get("stopped")):
        return True
    if time.monotonic() >= float(budget["deadline"]):
        budget["stopped"] = True
        return True
    if int(budget["files_scanned"]) >= int(budget["max_files"]):
        budget["stopped"] = True
        return True
    return False


def _consume_budget_file(budget: Optional[Dict[str, float | int | bool]]) -> bool:
    if _is_budget_exhausted(budget):
        return False
    if budget:
        budget["files_scanned"] = int(budget["files_scanned"]) + 1
    return True


def _prune_dirs_for_lookup(dirnames: List[str]) -> None:
    dirnames[:] = [d for d in dirnames if d.lower() not in _SOURCE_LOOKUP_SKIP_DIR_NAMES]


def _parse_faulting_module_function(analysis_text: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract MODULE_NAME and the bare function name from SYMBOL_NAME.

    For ``SYMBOL_NAME: MyAppCore!ProcessTreeNode+0x9e5`` this returns
    ``("MyAppCore", "ProcessTreeNode")``.
    """
    symbol_match = _SYMBOL_NAME_RE.search(analysis_text)
    module_match = _MODULE_NAME_RE.search(analysis_text)

    function_name = symbol_match.group(2) if symbol_match else None
    module_name = module_match.group(1) if module_match else None

    if function_name and "::" in function_name:
        function_name = function_name.rsplit("::", 1)[-1]

    return module_name, function_name


def _extract_stack_functions(analysis_text: str) -> List[Tuple[str, str]]:
    """Extract (module, bare_function_name) pairs from stack trace frames."""
    seen = set()
    results: List[Tuple[str, str]] = []
    # CDB-style frames: module!Function+0xOffset
    for module, symbol in _STACK_FRAME_RE.findall(analysis_text):
        bare = symbol.rsplit("::", 1)[-1] if "::" in symbol else symbol
        key = (module.lower(), bare.lower())
        if key not in seen:
            seen.add(key)
            results.append((module, bare))
    return results


def _parse_gdb_source_locations(
    analysis_text: str,
) -> List[Tuple[str, int]]:
    """Extract ``(file_path, line_number)`` pairs from GDB backtrace output.

    GDB emits ``at src/crash.cpp:15`` on every frame that has debug info.
    The first match is the innermost (crashing) frame — the most useful one.
    Returns unique ``(path, line)`` pairs in frame order.
    """
    seen: set = set()
    results: List[Tuple[str, int]] = []
    for m in _GDB_AT_RE.finditer(analysis_text):
        path, line_str = m.group(1), int(m.group(2))
        key = (os.path.basename(path).lower(), line_str)
        if key not in seen:
            seen.add(key)
            results.append((path, line_str))
    return results


def _extract_gdb_functions(analysis_text: str) -> List[str]:
    """Extract function names from GDB ``#N ... in func_name (`` frames.

    Strips C++ namespace prefixes (``foo::bar::Baz`` → ``Baz``).
    Skips obvious runtime/libc frames (``__libc_start_main``, ``??``, etc.).
    Returns unique names in stack order (frame 0 first).
    """
    _SKIP = frozenset({
        "??", "__libc_start_main", "__GI___libc_start_main",
        "_start", "__cxa_throw", "__cxa_allocate_exception",
    })
    seen: set = set()
    results: List[str] = []
    for m in _GDB_FRAME_FUNC_RE.finditer(analysis_text):
        raw = m.group(1)
        bare = raw.rsplit("::", 1)[-1] if "::" in raw else raw
        if bare in _SKIP or bare.startswith("__"):
            continue
        if bare not in seen:
            seen.add(bare)
            results.append(bare)
    return results


def _find_file_in_repo(
    filename: str,
    repo_path: str,
    budget: Optional[Dict[str, float | int | bool]] = None,
) -> List[str]:
    """Walk the repo to find files matching the given basename (ignores .gitignore).

    ``followlinks=False`` prevents symlinks from escaping the repo root.
    """
    matches = []
    target = filename.lower()
    for dirpath, dirnames, filenames in os.walk(repo_path, followlinks=False):
        _prune_dirs_for_lookup(dirnames)
        if _is_budget_exhausted(budget):
            logger.warning("Source lookup budget exhausted while searching for file %s", filename)
            break
        for f in filenames:
            if not _consume_budget_file(budget):
                logger.warning("Source lookup budget exhausted while scanning files for %s", filename)
                return matches
            if f.lower() == target:
                matches.append(os.path.join(dirpath, f))
    return matches


def _find_function_in_repo(
    function_name: str,
    repo_path: str,
    module_hint: Optional[str] = None,
    budget: Optional[Dict[str, float | int | bool]] = None,
) -> List[Tuple[str, int]]:
    """Search source files for a function definition."""
    patterns = [
        rf"^\s*[\w\s\*&:<>,]+\b{re.escape(function_name)}\s*\(",
        rf"::{re.escape(function_name)}\s*\(",
    ]
    combined = re.compile("|".join(patterns), re.MULTILINE)

    matches: List[Tuple[str, int]] = []

    for dirpath, dirnames, filenames in os.walk(repo_path, followlinks=False):
        _prune_dirs_for_lookup(dirnames)
        if _is_budget_exhausted(budget):
            logger.warning("Source lookup budget exhausted while searching for function %s", function_name)
            break
        for fname in filenames:
            if not _consume_budget_file(budget):
                logger.warning("Source lookup budget exhausted while scanning files for function %s", function_name)
                return matches
            if not any(fname.lower().endswith(ext) for ext in _SOURCE_EXTENSIONS):
                continue
            filepath = os.path.join(dirpath, fname)
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                    for line_num, line in enumerate(fh, 1):
                        if combined.search(line):
                            matches.append((filepath, line_num))
            except OSError:
                continue

    if module_hint and len(matches) > 1:
        hint_lower = module_hint.lower()
        matches.sort(key=lambda m: 0 if hint_lower in m[0].replace("\\", "/").lower() else 1)

    return matches


def _best_match(build_path: str, candidates: List[str]) -> str:
    """Pick the candidate whose suffix best matches the build-machine path."""
    if len(candidates) == 1:
        return candidates[0]

    build_parts = build_path.replace("\\", "/").lower().split("/")
    best, best_score = candidates[0], 0
    for candidate in candidates:
        cand_parts = candidate.replace("\\", "/").lower().split("/")
        score = 0
        for bp, cp in zip(reversed(build_parts), reversed(cand_parts)):
            if bp == cp:
                score += 1
            else:
                break
        if score > best_score:
            best, best_score = candidate, score
    return best


def _read_source_context(filepath: str, faulting_line: int, context: int = _SOURCE_CONTEXT_LINES) -> str:
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return f"(unable to read {filepath})"

    total = len(lines)
    start = max(0, faulting_line - context - 1)
    end = min(total, faulting_line + context)

    width = len(str(end))
    snippet_lines = []
    for idx in range(start, end):
        line_num = idx + 1
        marker = ">>>" if line_num == faulting_line else "   "
        snippet_lines.append(f"{marker} {line_num:>{width}} | {lines[idx].rstrip()}")
    return "\n".join(snippet_lines)


def _format_function_matches(
    matches: List[Tuple[str, int]],
    module_name: Optional[str],
    function_name: str,
    search_method: str,
    max_show: int = 3,
) -> str:
    result = (
        f"### Faulting Source Code (Located by {search_method})\n"
        f"- **Module:** `{module_name or 'Unknown'}`\n"
        f"- **Function:** `{function_name}`\n"
        f"- **Note:** Debug info lacks source line info; searched repo by function name\n\n"
    )

    for filepath, line_num in matches[:max_show]:
        result += f"#### Match: `{filepath}:{line_num}`\n"
        snippet = _read_source_context(filepath, line_num, context=15)
        result += f"```cpp\n{snippet}\n```\n\n"

    if len(matches) > max_show:
        result += f"*({len(matches) - max_show} more matches found)*\n\n"

    return result


def locate_faulting_source(analysis_text: str, repo_path: Optional[str]) -> Optional[str]:
    """Locate faulting source code using a multi-level fallback chain.

    Level 0 — **GDB/LLDB ``at file:line``** — extracts ``at src/foo.cpp:42``
    patterns directly from GDB backtrace output.  This is the richest source
    and is checked first because it avoids a full repo walk when file name
    and line are already known.

    Level 1 — **PDB/DWARF ``FAULTING_SOURCE_FILE``** — CDB/WinDbg structured
    debug-info output (``!analyze -v``).

    Level 2 — **CDB ``SYMBOL_NAME`` function search** — extracts the faulting
    function from ``SYMBOL_NAME: module!Function+0xOffset`` and greps for
    its definition across the repo.

    Level 3a — **CDB stack-frame function search** — walks every
    ``module!Function+0xOffset`` frame and searches for definitions.

    Level 3b — **GDB frame function search** — walks every ``#N ... in func (``
    frame from GDB output; used when Level 0 found no file matches and no CDB
    frames were present.

    All levels search the entire repo tree (including gitignored directories)
    so shared-component source is discoverable.
    """
    if not repo_path:
        return None

    repo_path = os.path.abspath(repo_path)
    if not os.path.isdir(repo_path):
        return None
    budget = _new_source_lookup_budget()

    # ----- Level 0: GDB/LLDB "at file.cpp:line" (highest fidelity) -----
    gdb_locations = _parse_gdb_source_locations(analysis_text)
    for gdb_path, gdb_line in gdb_locations:
        if _is_budget_exhausted(budget):
            break
        filename = os.path.basename(gdb_path)
        candidates = _find_file_in_repo(filename, repo_path, budget)
        if candidates:
            best = _best_match(gdb_path, candidates)
            header = (
                f"### Faulting Source Code\n"
                f"- **Source:** `{gdb_path}` (from GDB debug info)\n"
                f"- **Local path:** `{best}`\n"
                f"- **Faulting line:** {gdb_line}\n"
            )
            snippet = _read_source_context(best, gdb_line)
            return header + f"\n```cpp\n{snippet}\n```\n\n"
        logger.debug("Level 0: GDB file %s not found in repo", filename)

    # ----- Level 1: PDB/DWARF FAULTING_SOURCE_FILE (CDB output) -----
    faulting_file, faulting_line = _parse_faulting_source(analysis_text)
    if faulting_file:
        filename = os.path.basename(faulting_file)
        candidates = _find_file_in_repo(filename, repo_path, budget)
        if candidates:
            best = _best_match(faulting_file, candidates)
            header = (
                f"### Faulting Source Code\n"
                f"- **Build path:** `{faulting_file}`\n"
                f"- **Local path:** `{best}`\n"
                f"- **Faulting line:** {faulting_line}\n"
            )
            if faulting_line:
                snippet = _read_source_context(best, faulting_line)
                return header + f"\n```cpp\n{snippet}\n```\n\n"
            return header + "\n"
        logger.info("Level 1: source file %s not found in repo", filename)

    # ----- Level 2: CDB SYMBOL_NAME function search -----
    module_name, function_name = _parse_faulting_module_function(analysis_text)
    if function_name:
        logger.info("Level 2: searching for function %s (module %s)", function_name, module_name)
        matches = _find_function_in_repo(function_name, repo_path, module_name, budget)
        if matches:
            return _format_function_matches(matches, module_name, function_name, "Symbol Name Search")

    # ----- Level 3a: CDB module!Function stack-frame search -----
    stack_functions = _extract_stack_functions(analysis_text)
    for frame_module, frame_func in stack_functions:
        if _is_budget_exhausted(budget):
            break
        matches = _find_function_in_repo(frame_func, repo_path, frame_module, budget)
        if matches:
            return _format_function_matches(matches, frame_module, frame_func, "Stack Trace Search")

    # ----- Level 3b: GDB frame function search -----
    if not _is_budget_exhausted(budget):
        gdb_functions = _extract_gdb_functions(analysis_text)
        for func_name in gdb_functions:
            if _is_budget_exhausted(budget):
                break
            matches = _find_function_in_repo(func_name, repo_path, None, budget)
            if matches:
                return _format_function_matches(matches, None, func_name, "GDB Frame Search")

    # ----- Nothing found -----
    if bool(budget.get("stopped")):
        return (
            f"### Faulting Source Code\n"
            f"Source lookup budget exhausted before finding a confident match.\n"
            f"- Repo path: `{repo_path}`\n"
            f"- Files scanned: `{int(budget['files_scanned'])}`\n"
            f"- Time budget: `{_SOURCE_LOOKUP_MAX_SECONDS}s`\n"
            f"- File budget: `{_SOURCE_LOOKUP_MAX_FILES}`\n\n"
            f"Try narrowing `repo_path` to the most relevant module subtree.\n"
        )
    # gdb_functions is only defined when Level 3b ran (budget not exhausted then)
    _gdb_funcs_found = (not _is_budget_exhausted(budget)) and bool(
        locals().get("gdb_functions")
    )
    if function_name or faulting_file or gdb_locations or _gdb_funcs_found:
        first_gdb = gdb_locations[0][0] if gdb_locations else None
        return (
            f"### Faulting Source Code\n"
            f"Could not locate source in `{repo_path}`.\n"
            f"- Module: `{module_name or 'Unknown'}`\n"
            f"- Function: `{function_name or 'Unknown'}`\n"
            f"- Build path: `{faulting_file or first_gdb or 'N/A'}`\n\n"
        )
    return None


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


def _evict_lru_session() -> None:
    """Evict the least-recently-used session.  Caller must hold ``_session_lock``."""
    if not active_sessions:
        return
    oldest_id, oldest_session = next(iter(active_sessions.items()))
    logger.info(
        "Evicting LRU session %s (pool limit %d reached)",
        oldest_id,
        _max_concurrent_sessions,
    )
    try:
        if oldest_session is not None:
            oldest_session.shutdown()
    except Exception:
        pass
    del active_sessions[oldest_id]


def get_or_create_session(
    dump_path: str,
    cdb_path: Optional[str] = None,
    debugger_path: Optional[str] = None,
    debugger_type: str = "auto",
    symbols_path: Optional[str] = None,
    image_path: Optional[str] = None,
    replace_if_config_mismatch: bool = False,
    timeout: int = 30,
    verbose: bool = False,
    **_kwargs,
) -> DebuggerSession:
    """Get an existing session or create a new one using the backend factory.

    Enforces ``_max_concurrent_sessions``: if the limit is reached the
    least-recently-used session is evicted.

    The entire check-and-create sequence is serialised by ``_session_lock``
    to prevent two concurrent callers from both determining a session is
    missing and both attempting to create it.
    """
    if not dump_path:
        raise ValueError("dump_path must be provided")

    session_id = os.path.abspath(dump_path)
    effective_debugger_path = debugger_path or cdb_path

    with _session_lock:
        existing = active_sessions.get(session_id)
        config_mismatch = existing is not None and (
            (existing.symbols_path or "") != (symbols_path or "")
            or (existing.image_path or "") != (image_path or "")
        )

        if config_mismatch and replace_if_config_mismatch:
            try:
                existing.shutdown()
            except Exception:
                pass
            finally:
                active_sessions[session_id] = None

        if session_id not in active_sessions or active_sessions[session_id] is None:
            while active_session_count() >= _max_concurrent_sessions:
                _evict_lru_session()

            try:
                session = create_session(
                    dump_path=dump_path,
                    debugger_path=effective_debugger_path,
                    symbols_path=symbols_path,
                    image_path=image_path,
                    timeout=timeout,
                    verbose=verbose,
                    debugger_type=debugger_type,
                )
                active_sessions[session_id] = session
            except Exception as e:
                raise McpError(
                    ErrorData(code=INTERNAL_ERROR, message=f"Failed to create session: {e}")
                )
        elif config_mismatch and not replace_if_config_mismatch:
            pass  # return existing session even though config differs
        else:
            active_sessions.move_to_end(session_id)

        return active_sessions[session_id]


def close_session(dump_path: str) -> bool:
    if not dump_path:
        return False

    session_id = os.path.abspath(dump_path)

    with _session_lock:
        if session_id in active_sessions and active_sessions[session_id] is not None:
            try:
                active_sessions[session_id].shutdown()
            except Exception:
                pass
            finally:
                del active_sessions[session_id]
            return True
    return False


def cleanup_all_sessions() -> None:
    for session in active_sessions.values():
        try:
            if session is not None:
                session.shutdown()
        except Exception:
            pass
    active_sessions.clear()


# ---------------------------------------------------------------------------
# Dump analysis helper
# ---------------------------------------------------------------------------


async def _run_dump_analysis(
    args,
    *,
    cdb_path: Optional[str],
    debugger_path: Optional[str] = None,
    debugger_type: str = "auto",
    symbols_path: Optional[str],
    image_path: Optional[str],
    repo_path: Optional[str],
    timeout: int,
    verbose: bool,
) -> list[TextContent]:
    """Run the standard dump analysis pipeline and return markdown output."""
    effective_symbols_path = args.symbols_path or symbols_path
    effective_image_path = args.image_path or image_path
    force_replace = args.symbols_path is not None or args.image_path is not None

    session = await asyncio.to_thread(
        get_or_create_session,
        dump_path=args.dump_path,
        cdb_path=cdb_path,
        debugger_path=debugger_path,
        debugger_type=debugger_type,
        symbols_path=effective_symbols_path,
        image_path=effective_image_path,
        replace_if_config_mismatch=force_replace,
        timeout=timeout,
        verbose=verbose,
    )

    results = []
    crash_info = await asyncio.to_thread(session.get_crash_info)
    results.append("### Crash Information\n```\n" + crash_info + "\n```\n\n")

    analysis = await asyncio.to_thread(session.run_crash_analysis)
    results.append("### Crash Analysis\n```\n" + analysis + "\n```\n\n")

    effective_repo_path = args.repo_path or repo_path
    if effective_repo_path:
        source_section = await asyncio.to_thread(locate_faulting_source, analysis, effective_repo_path)
        if source_section:
            results.append(source_section)

    if args.include_stack_trace:
        stack = await asyncio.to_thread(session.get_stack_trace)
        results.append("### Stack Trace\n```\n" + stack + "\n```\n\n")

    if args.include_modules:
        modules = await asyncio.to_thread(session.get_loaded_modules)
        results.append("### Loaded Modules\n```\n" + modules + "\n```\n\n")

    if args.include_threads:
        threads = await asyncio.to_thread(session.get_threads)
        results.append("### Threads\n```\n" + threads + "\n```\n\n")

    return [TextContent(type="text", text="".join(results))]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def _dump_path_hint(debugger_type: str = "auto") -> str:
    local_path = get_local_dumps_path(debugger_type)
    hint = ""
    if local_path:
        patterns = _dump_file_patterns(debugger_type)
        dumps = []
        for pat in patterns:
            dumps.extend(glob.glob(os.path.join(local_path, pat)))
        if dumps:
            hint = f"\n\nFound {len(dumps)} dump(s) in {local_path}:\n"
            for i, d in enumerate(dumps[:10]):
                try:
                    size = round(os.path.getsize(d) / (1024 * 1024), 2)
                except OSError:
                    size = "?"
                hint += f"  {i+1}. {d} ({size} MB)\n"
    return hint


async def handle_analyze_dump(
    arguments: dict,
    *,
    cdb_path: Optional[str],
    debugger_path: Optional[str] = None,
    debugger_type: str = "auto",
    symbols_path: Optional[str],
    image_path: Optional[str],
    repo_path: Optional[str],
    timeout: int,
    verbose: bool,
    AnalyzeDumpParams,
) -> list[TextContent]:
    if "dump_path" not in arguments or not arguments.get("dump_path"):
        return [TextContent(type="text", text=f"Please provide a dump_path.{_dump_path_hint(debugger_type)}")]

    args = AnalyzeDumpParams(**arguments)
    return await _run_dump_analysis(
        args,
        cdb_path=cdb_path,
        debugger_path=debugger_path,
        debugger_type=debugger_type,
        symbols_path=symbols_path,
        image_path=image_path,
        repo_path=repo_path,
        timeout=timeout,
        verbose=verbose,
    )


async def handle_open_dump(
    arguments: dict,
    *,
    cdb_path: Optional[str],
    debugger_path: Optional[str] = None,
    debugger_type: str = "auto",
    symbols_path: Optional[str],
    image_path: Optional[str],
    repo_path: Optional[str],
    timeout: int,
    verbose: bool,
    OpenDumpParams,
) -> list[TextContent]:
    if "dump_path" not in arguments or not arguments.get("dump_path"):
        return [TextContent(type="text", text=f"Please provide a dump_path.{_dump_path_hint(debugger_type)}")]

    args = OpenDumpParams(**arguments)
    return await _run_dump_analysis(
        args,
        cdb_path=cdb_path,
        debugger_path=debugger_path,
        debugger_type=debugger_type,
        symbols_path=symbols_path,
        image_path=image_path,
        repo_path=repo_path,
        timeout=timeout,
        verbose=verbose,
    )


async def handle_run_cmd(
    arguments: dict,
    *,
    cdb_path: Optional[str],
    debugger_path: Optional[str] = None,
    debugger_type: str = "auto",
    symbols_path: Optional[str],
    image_path: Optional[str],
    repo_path: Optional[str] = None,  # accepted but not used — keeps **debugger_ctx passthrough clean
    timeout: int,
    verbose: bool,
    RunCommandParams,
    **_extra,
) -> list[TextContent]:
    args = RunCommandParams(**arguments)

    validate_debugger_command(args.command, debugger_type)

    if not _cmd_rate_limiter.consume():
        raise McpError(
            ErrorData(
                code=INVALID_PARAMS,
                message="Rate limit exceeded for run_debugger_cmd. Please wait before retrying.",
            )
        )

    logger.info("run_debugger_cmd: command=%s dump=%s", args.command, args.dump_path)

    effective_symbols_path = args.symbols_path or symbols_path
    effective_image_path = args.image_path or image_path
    force_replace = args.symbols_path is not None or args.image_path is not None

    session = await asyncio.to_thread(
        get_or_create_session,
        dump_path=args.dump_path,
        cdb_path=cdb_path,
        debugger_path=debugger_path,
        debugger_type=debugger_type,
        symbols_path=effective_symbols_path,
        image_path=effective_image_path,
        replace_if_config_mismatch=force_replace,
        timeout=timeout,
        verbose=verbose,
    )
    output = await asyncio.to_thread(session.send_command, args.command, args.timeout)
    return [TextContent(type="text", text=f"Command: {args.command}\n\n```\n" + "\n".join(output) + "\n```")]


async def handle_close_dump(arguments: dict, *, CloseDumpParams) -> list[TextContent]:
    args = CloseDumpParams(**arguments)
    success = await asyncio.to_thread(close_session, dump_path=args.dump_path)
    msg = f"Closed: {args.dump_path}" if success else f"No active session: {args.dump_path}"
    return [TextContent(type="text", text=msg)]


async def handle_list_dumps(
    arguments: dict,
    *,
    debugger_type: str = "auto",
    ListDumpsParams,
) -> list[TextContent]:
    args = ListDumpsParams(**arguments)

    search_dir = args.directory_path or get_local_dumps_path(debugger_type)
    if not search_dir:
        raise McpError(ErrorData(code=INVALID_PARAMS, message="No directory specified and no default found"))

    if not os.path.isdir(search_dir):
        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Directory not found: {search_dir}"))

    patterns = _dump_file_patterns(debugger_type)
    dumps = []
    for pat in patterns:
        if args.recursive:
            dumps.extend(glob.glob(os.path.join(search_dir, "**", pat), recursive=True))
        else:
            dumps.extend(glob.glob(os.path.join(search_dir, pat)))
    dumps = sorted(set(dumps))

    if not dumps:
        return [TextContent(type="text", text=f"No dump files found in {search_dir}")]

    result = f"Found {len(dumps)} dump(s) in {search_dir}:\n\n"
    for i, d in enumerate(dumps):
        try:
            size = round(os.path.getsize(d) / (1024 * 1024), 2)
        except OSError:
            size = "?"
        result += f"{i+1}. {d} ({size} MB)\n"

    return [TextContent(type="text", text=result)]
