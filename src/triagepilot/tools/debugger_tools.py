"""Debugger tool handlers for the MCP server (platform-agnostic)."""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import sys
import threading
import time
from collections import OrderedDict

from mcp.shared.exceptions import McpError
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, ErrorData, TextContent

from ..backends import (
    DebuggerSession,
    create_session,
    detect_debugger_type,
)
from ..backends import (
    get_local_dumps_path as _backend_get_local_dumps_path,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Security: command blocklist & rate limiting
# ---------------------------------------------------------------------------

BLOCKED_COMMAND_PREFIXES_CDB = (
    ".shell",
    ".create",
    ".write",
    ".crash",
    ".reboot",
    ".kill",
    "!bpset",
    ".load",
    ".unload",  # arbitrary DLL loading
    ".logopen",
    ".logclose",  # file system writes
    ".writemem",  # write memory to file
    ".dump",  # create dump file
)
BLOCKED_COMMAND_PREFIXES_GDB = (
    "shell",
    "!",
    "python",
    "python-interactive",
    "pi",
    "guile",
    "guile-repl",
    "call",
    "define",
    "source",
)
BLOCKED_COMMAND_PREFIXES_LLDB = (
    "platform shell",
    "process launch",
    "script",
)


def validate_debugger_command(command: str, debugger_type: str = "auto") -> None:
    """Reject debugger commands on the security blocklist."""
    # Every backend appends its own trailing "echo <marker>" command after a
    # "\n" to detect completion (see send_command() in each backends/*.py).
    # An embedded newline/CR in `command` would let arbitrary extra commands
    # ride along after it, bypassing the blocklist below entirely.
    if "\n" in command or "\r" in command:
        logger.warning("Blocked debugger command attempt with embedded line break: %r", command)
        raise McpError(
            ErrorData(
                code=INVALID_PARAMS,
                message="Command must not contain newlines or carriage returns.",
            )
        )

    if debugger_type == "auto":
        debugger_type = detect_debugger_type()

    blocklist: tuple[str, ...]
    if debugger_type == "cdb":
        blocklist = BLOCKED_COMMAND_PREFIXES_CDB
    elif debugger_type == "gdb":
        blocklist = BLOCKED_COMMAND_PREFIXES_GDB
    elif debugger_type == "lldb":
        blocklist = BLOCKED_COMMAND_PREFIXES_LLDB
    else:
        blocklist = BLOCKED_COMMAND_PREFIXES_CDB

    # CDB treats ';' as a same-line command separator, which would otherwise
    # let a blocked command hide behind an allowed one, e.g. "r;.shell calc".
    segments = command.split(";") if debugger_type == "cdb" else [command]

    for segment in segments:
        normalized = segment.strip().lower()
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


def _resolve_scoped_path(candidate: str | None, base: str | None, param_name: str) -> str | None:
    """Resolve a per-call path override, confined to the configured *base* root.

    ``repo_path``/``symbols_path`` accept a per-call override (see
    ``AnalyzeDumpParams`` etc. in server.py), and source localization echoes
    matched file contents back into the MCP response. Without confinement, a
    caller (or a crash-triage prompt steered by attacker-controlled dump
    content) could point ``repo_path`` outside the intended project and read
    back arbitrary files the server process has access to. When the operator
    has configured a default root (``base``), any override must resolve
    inside it. With no configured default there is no boundary to enforce,
    so *candidate* passes through unchanged (matches prior behavior).
    """
    if not candidate or not base:
        return candidate or base

    real_base = os.path.realpath(base)
    real_candidate = os.path.realpath(candidate)
    if real_candidate != real_base and not real_candidate.startswith(real_base + os.sep):
        raise McpError(
            ErrorData(
                code=INVALID_PARAMS,
                message=(
                    f"{param_name} '{candidate}' is outside the configured root "
                    f"'{base}'. Use a path within the configured root."
                ),
            )
        )
    return candidate


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


def get_local_dumps_path(debugger_type: str = "auto") -> str | None:
    """Get the default crash dumps path for the current platform."""
    return _backend_get_local_dumps_path(debugger_type)


def _dump_file_patterns(debugger_type: str = "auto") -> list[str]:
    """Return glob patterns for crash dump files based on platform."""
    if debugger_type == "auto":
        debugger_type = detect_debugger_type()
    if debugger_type == "cdb":
        # *.*dmp matches .dmp, .mdmp (minidump), .hdmp (heap dump), .kdmp (kernel dump)
        # *.cab matches Windows Error Reporting compressed dumps
        return ["*.*dmp", "*.cab"]
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
_SOURCE_LOOKUP_MAX_SECONDS = max(
    5, int(os.environ.get("TRIAGEPILOT_SOURCE_LOOKUP_MAX_SECONDS", "45"))
)
_SOURCE_LOOKUP_MAX_FILES = max(
    1000, int(os.environ.get("TRIAGEPILOT_SOURCE_LOOKUP_MAX_FILES", "200000"))
)
_SOURCE_LOOKUP_SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".vs",
        ".idea",
        ".cache",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        "node_modules",
        # Python's equivalent of node_modules -- vendored dependency source,
        # not the user's own code. Without these, a generic function name
        # shared with some dependency (confirmed live: "abort" also appears
        # in a vendored mypyc runtime under .venv/) can rank a third-party
        # match ahead of -- or instead of -- the real user-code frame
        # several levels down the fallback chain never gets tried.
        ".venv",
        "venv",
        "site-packages",
        "dist-packages",
        ".tox",
    }
)

_SOURCE_EXTENSIONS = frozenset(
    (
        ".cpp",
        ".c",
        ".cc",
        ".cxx",
        ".h",
        ".hpp",
        ".hxx",
        ".inl",
        ".m",
        ".mm",  # Objective-C / Objective-C++
        ".swift",  # Swift
        ".rs",  # Rust
        ".go",  # Go
    )
)

_SYMBOL_NAME_RE = re.compile(r"^SYMBOL_NAME:\s+(\w+)!([A-Za-z0-9_:~]+)", re.MULTILINE)
_MODULE_NAME_RE = re.compile(r"^MODULE_NAME:\s+(\w+)", re.MULTILINE)
_STACK_FRAME_RE = re.compile(r"(\w+)!([A-Za-z0-9_:~]+)\+0x[0-9a-fA-F]+")

# Windows system modules that can appear as a CDB frame's module -- never the
# user's own code, so searching the repo for "a definition of the function
# this frame is in" is always pointless, and for a common CRT/libc name that
# also happens to be *called* or *mentioned* elsewhere in the user's own code
# (confirmed live: "abort" -- both a real call site and a comment mentioning
# it elsewhere in this repo's examples), actively misleading: it returns a
# same-named but unrelated match instead of falling through to a real
# user-code frame further down the stack.
_CDB_SYSTEM_MODULES = frozenset(
    {
        "ntdll",
        "kernel32",
        "kernelbase",
        "ucrtbase",
        "ucrtbased",
        "msvcrt",
        "msvcrtd",
        "vcruntime140",
        "vcruntime140d",
        "vcruntime140_1",
        "combase",
        "rpcrt4",
        "advapi32",
        "sechost",
    }
)

# Well-known CRT/runtime function names that are essentially never
# user-defined, only called -- the module-name check above doesn't catch
# these when the CRT is statically linked (this repo's /MT builds): a frame
# like "double_free!abort" attributes CRT code to the *user's own* module
# name, not ucrtbase/msvcrt. Without this, a bare call site with leading
# whitespace satisfies _find_function_in_repo's "return type prefix" pattern
# just as well as a real definition (confirmed live: "    abort();" in
# lock-order-inversion-deadlock.cpp matched a search for "abort" triggered
# while locating source for a *different* example's faulting frame).
# Mirrors the skip lists signature.py's _SKIP_FUNCTIONS and this module's
# own _extract_gdb_functions() already use for the same reason on other
# backends.
_RUNTIME_FUNCTION_NAMES = frozenset(
    {
        "abort",
        "raise",
        "terminate",
        "exit",
        "_exit",
        "quick_exit",
        "malloc",
        "calloc",
        "realloc",
        "free",
        "memcpy",
        "memmove",
        "memset",
        "strcpy",
        "strncpy",
        "strcat",
        "longjmp",
        "_cxxthrowexception",
    }
)

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


def _parse_faulting_source(analysis_text: str) -> tuple[str | None, int | None]:
    """Extract FAULTING_SOURCE_FILE and FAULTING_SOURCE_LINE_NUMBER from analysis output."""
    file_match = _FAULTING_FILE_RE.search(analysis_text)
    line_match = _FAULTING_LINE_RE.search(analysis_text)
    faulting_file = file_match.group(1).strip() if file_match else None
    faulting_line = int(line_match.group(1)) if line_match else None
    return faulting_file, faulting_line


def _new_source_lookup_budget() -> dict[str, float | int | bool]:
    return {
        "deadline": time.monotonic() + _SOURCE_LOOKUP_MAX_SECONDS,
        "max_files": _SOURCE_LOOKUP_MAX_FILES,
        "files_scanned": 0,
        "stopped": False,
    }


def _is_budget_exhausted(budget: dict[str, float | int | bool] | None) -> bool:
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


def _consume_budget_file(budget: dict[str, float | int | bool] | None) -> bool:
    if _is_budget_exhausted(budget):
        return False
    if budget:
        budget["files_scanned"] = int(budget["files_scanned"]) + 1
    return True


def _prune_dirs_for_lookup(dirnames: list[str]) -> None:
    dirnames[:] = [d for d in dirnames if d.lower() not in _SOURCE_LOOKUP_SKIP_DIR_NAMES]


def _parse_faulting_module_function(analysis_text: str) -> tuple[str | None, str | None]:
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


def _extract_stack_functions(analysis_text: str) -> list[tuple[str, str]]:
    """Extract (module, bare_function_name) pairs from stack trace frames."""
    seen = set()
    results: list[tuple[str, str]] = []
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
) -> list[tuple[str, int]]:
    """Extract ``(file_path, line_number)`` pairs from GDB backtrace output.

    GDB emits ``at src/crash.cpp:15`` on every frame that has debug info.
    The first match is the innermost (crashing) frame — the most useful one.
    Returns unique ``(path, line)`` pairs in frame order.
    """
    seen: set = set()
    results: list[tuple[str, int]] = []
    for m in _GDB_AT_RE.finditer(analysis_text):
        path, line_str = m.group(1), int(m.group(2))
        key = (os.path.basename(path).lower(), line_str)
        if key not in seen:
            seen.add(key)
            results.append((path, line_str))
    return results


def _extract_gdb_functions(analysis_text: str) -> list[str]:
    """Extract function names from GDB ``#N ... in func_name (`` frames.

    Strips C++ namespace prefixes (``foo::bar::Baz`` → ``Baz``).
    Skips obvious runtime/libc frames (``__libc_start_main``, ``??``, etc.).
    Returns unique names in stack order (frame 0 first).
    """
    _SKIP = frozenset(
        {
            "??",
            "__libc_start_main",
            "__GI___libc_start_main",
            "_start",
            "__cxa_throw",
            "__cxa_allocate_exception",
        }
    )
    seen: set = set()
    results: list[str] = []
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
    budget: dict[str, float | int | bool] | None = None,
) -> list[str]:
    """Walk the repo to find files matching the given basename (ignores .gitignore).

    ``followlinks=False`` keeps the walk from descending into symlinked
    *directories*, but ``os.walk`` still lists symlinked *files* -- skip
    those explicitly so a symlink planted in the repo can't be used to read
    a file outside ``repo_path``.
    """
    matches: list[str] = []
    target = filename.lower()
    for dirpath, dirnames, filenames in os.walk(repo_path, followlinks=False):
        _prune_dirs_for_lookup(dirnames)
        if _is_budget_exhausted(budget):
            logger.warning("Source lookup budget exhausted while searching for file %s", filename)
            break
        for f in filenames:
            if not _consume_budget_file(budget):
                logger.warning(
                    "Source lookup budget exhausted while scanning files for %s", filename
                )
                return matches
            full_path = os.path.join(dirpath, f)
            if f.lower() == target and not os.path.islink(full_path):
                matches.append(full_path)
    return matches


def _strip_comment(line: str, in_block_comment: bool) -> tuple[str, bool]:
    """Best-effort strip C/C++ comments from *line* for function-search matching.

    Not a real tokenizer (doesn't know about "//" or "/*" inside a string or
    char literal), but good enough to stop a function name merely *mentioned*
    in a comment from being mistaken for its definition. Confirmed live: a
    comment ("fire abort()/SIGQUIT on a hung process") in
    lock-order-inversion-deadlock.cpp false-matched a search for "abort" (the
    faulting frame's library function) triggered while locating source for a
    *different* example, and matches for a generic name like that are
    otherwise indistinguishable from a real one-line definition.

    Returns ``(cleaned_line, still_in_block_comment)`` -- the caller carries
    the returned state into the next line for multi-line ``/* ... */`` blocks.
    """
    result: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        if in_block_comment:
            end = line.find("*/", i)
            if end == -1:
                return "".join(result), True
            in_block_comment = False
            i = end + 2
            continue
        two = line[i : i + 2]
        if two == "//":
            break
        if two == "/*":
            i += 2
            in_block_comment = True
            continue
        result.append(line[i])
        i += 1
    return "".join(result), in_block_comment


def _find_function_in_repo(
    function_name: str,
    repo_path: str,
    module_hint: str | None = None,
    budget: dict[str, float | int | bool] | None = None,
) -> list[tuple[str, int]]:
    """Search source files for a function definition."""
    if function_name.startswith("~"):
        # Destructor. The generic pattern below requires a `\b` immediately
        # before the (escaped) name to end the "return type" prefix it
        # consumes -- but `~` is a non-word character, so there's never a
        # \w/\W transition between it and preceding whitespace for `\b` to
        # anchor on, and a destructor defined inline in the class body
        # ("~Foo() {", no "ClassName::" qualifier) has no return type to
        # match there anyway. Confirmed live: CDB stack frames for
        # exception-in-destructor-terminate.cpp's inline `~ScopedTransaction`
        # never matched either of the patterns below.
        patterns = [rf"(?:^|\s|::){re.escape(function_name)}\s*\("]
    else:
        patterns = [
            rf"^\s*[\w\s\*&:<>,]+\b{re.escape(function_name)}\s*\(",
            rf"::{re.escape(function_name)}\s*\(",
        ]
    combined = re.compile("|".join(patterns), re.MULTILINE)

    matches: list[tuple[str, int]] = []

    for dirpath, dirnames, filenames in os.walk(repo_path, followlinks=False):
        _prune_dirs_for_lookup(dirnames)
        if _is_budget_exhausted(budget):
            logger.warning(
                "Source lookup budget exhausted while searching for function %s", function_name
            )
            break
        for fname in filenames:
            if not _consume_budget_file(budget):
                logger.warning(
                    "Source lookup budget exhausted while scanning files for function %s",
                    function_name,
                )
                return matches
            if not any(fname.lower().endswith(ext) for ext in _SOURCE_EXTENSIONS):
                continue
            filepath = os.path.join(dirpath, fname)
            if os.path.islink(filepath):
                continue
            try:
                in_block_comment = False
                with open(filepath, encoding="utf-8", errors="replace") as fh:
                    for line_num, line in enumerate(fh, 1):
                        cleaned, in_block_comment = _strip_comment(line, in_block_comment)
                        if combined.search(cleaned):
                            matches.append((filepath, line_num))
            except OSError:
                continue

    if module_hint and len(matches) > 1:
        # Strip -/_ before comparing: a PE/COFF module name can't contain a
        # hyphen, so CDB reports MSVC-built "double-free.exe" as module
        # "double_free" -- a plain substring check ("double_free" in
        # ".../double-free.cpp") never matches, silently leaving the hint
        # unused for exactly the repos (like this one) that name examples
        # with hyphens. Confirmed live: without this, a generic function
        # name defined in many files (e.g. every example's `main`) ranked
        # double-free.cpp's own match below the first max_show=3 shown,
        # so locate_faulting_source() never displayed it at all.
        hint_norm = module_hint.lower().replace("-", "").replace("_", "")
        matches.sort(
            key=lambda m: (
                0
                if hint_norm in m[0].replace("\\", "/").lower().replace("-", "").replace("_", "")
                else 1
            )
        )

    return matches


def _best_match(build_path: str, candidates: list[str]) -> str:
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


def _read_source_context(
    filepath: str, faulting_line: int, context: int = _SOURCE_CONTEXT_LINES
) -> str:
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
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
    matches: list[tuple[str, int]],
    module_name: str | None,
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


def locate_faulting_source(analysis_text: str, repo_path: str | None) -> str | None:
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
    if (
        function_name
        and function_name.lower() not in _RUNTIME_FUNCTION_NAMES
        and (module_name or "").lower() not in _CDB_SYSTEM_MODULES
    ):
        logger.info("Level 2: searching for function %s (module %s)", function_name, module_name)
        matches = _find_function_in_repo(function_name, repo_path, module_name, budget)
        if matches:
            return _format_function_matches(
                matches, module_name, function_name, "Symbol Name Search"
            )

    # ----- Level 3a: CDB module!Function stack-frame search -----
    stack_functions = _extract_stack_functions(analysis_text)
    for frame_module, frame_func in stack_functions:
        if _is_budget_exhausted(budget):
            break
        if frame_module.lower() in _CDB_SYSTEM_MODULES:
            continue
        if frame_func.lower() in _RUNTIME_FUNCTION_NAMES:
            continue
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
    _gdb_funcs_found = (not _is_budget_exhausted(budget)) and bool(locals().get("gdb_functions"))
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
        logger.warning("Error shutting down session %s during eviction", oldest_id, exc_info=True)
    finally:
        active_sessions.pop(oldest_id, None)


def get_or_create_session(
    dump_path: str,
    cdb_path: str | None = None,
    debugger_path: str | None = None,
    debugger_type: str = "auto",
    symbols_path: str | None = None,
    image_path: str | None = None,
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

        if config_mismatch and replace_if_config_mismatch and existing is not None:
            try:
                existing.shutdown()
            except Exception:
                pass
            finally:
                del active_sessions[session_id]

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
    for session_id in list(active_sessions):
        session = active_sessions.pop(session_id, None)
        if session is not None:
            try:
                session.shutdown()
            except Exception:
                logger.warning(
                    "Error shutting down session %s during cleanup", session_id, exc_info=True
                )


# ---------------------------------------------------------------------------
# Dump analysis helper
# ---------------------------------------------------------------------------


async def _run_dump_analysis(
    args,
    *,
    cdb_path: str | None,
    debugger_path: str | None = None,
    debugger_type: str = "auto",
    symbols_path: str | None,
    image_path: str | None,
    repo_path: str | None,
    timeout: int,
    verbose: bool,
) -> list[TextContent]:
    """Run the standard dump analysis pipeline and return markdown output."""
    effective_symbols_path = _resolve_scoped_path(args.symbols_path, symbols_path, "symbols_path")
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

    effective_repo_path = _resolve_scoped_path(args.repo_path, repo_path, "repo_path")
    if effective_repo_path:
        source_section = await asyncio.to_thread(
            locate_faulting_source, analysis, effective_repo_path
        )
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
                    size_str = str(round(os.path.getsize(d) / (1024 * 1024), 2))
                except OSError:
                    size_str = "?"
                hint += f"  {i + 1}. {d} ({size_str} MB)\n"
    return hint


async def handle_analyze_dump(
    arguments: dict,
    *,
    cdb_path: str | None,
    debugger_path: str | None = None,
    debugger_type: str = "auto",
    symbols_path: str | None,
    image_path: str | None,
    repo_path: str | None,
    timeout: int,
    verbose: bool,
    AnalyzeDumpParams,
    memory_store=None,
    memory_auto_recall: bool = True,
    memory_auto_save: bool = True,
) -> list[TextContent]:
    if "dump_path" not in arguments or not arguments.get("dump_path"):
        return [
            TextContent(
                type="text", text=f"Please provide a dump_path.{_dump_path_hint(debugger_type)}"
            )
        ]

    args = AnalyzeDumpParams(**arguments)
    results = await _run_dump_analysis(
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

    # Memory integration: auto-recall and auto-save
    if memory_store is not None:
        try:
            from ..memory.tools import auto_recall_similar, auto_save_analysis

            full_text = "".join(r.text for r in results)

            # Auto-recall: prepend similar past crashes
            if memory_auto_recall:
                recall_section = auto_recall_similar(memory_store, full_text, limit=3)
                if recall_section:
                    results.insert(0, TextContent(type="text", text=recall_section))

            # Auto-save: store this analysis for future recall
            if memory_auto_save:
                import sys

                platform = (
                    "windows"
                    if sys.platform == "win32"
                    else "macos"
                    if sys.platform == "darwin"
                    else "linux"
                )
                auto_save_analysis(
                    memory_store,
                    dump_path=args.dump_path,
                    analysis_text=full_text,
                    debugger_type=debugger_type,
                    platform=platform,
                )
        except Exception:
            logger.warning("Memory integration failed during analyze_dump", exc_info=True)

    return results


async def handle_open_dump(
    arguments: dict,
    *,
    cdb_path: str | None,
    debugger_path: str | None = None,
    debugger_type: str = "auto",
    symbols_path: str | None,
    image_path: str | None,
    repo_path: str | None,
    timeout: int,
    verbose: bool,
    OpenDumpParams,
) -> list[TextContent]:
    if "dump_path" not in arguments or not arguments.get("dump_path"):
        return [
            TextContent(
                type="text", text=f"Please provide a dump_path.{_dump_path_hint(debugger_type)}"
            )
        ]

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
    cdb_path: str | None,
    debugger_path: str | None = None,
    debugger_type: str = "auto",
    symbols_path: str | None,
    image_path: str | None,
    repo_path: str | None = None,  # accepted but not used — keeps **debugger_ctx passthrough clean
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

    effective_symbols_path = _resolve_scoped_path(args.symbols_path, symbols_path, "symbols_path")
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
    return [
        TextContent(
            type="text", text=f"Command: {args.command}\n\n```\n" + "\n".join(output) + "\n```"
        )
    ]


async def handle_close_dump(arguments: dict, *, CloseDumpParams) -> list[TextContent]:
    args = CloseDumpParams(**arguments)
    success = await asyncio.to_thread(close_session, dump_path=args.dump_path)
    msg = f"Closed: {args.dump_path}" if success else f"No active session: {args.dump_path}"
    return [TextContent(type="text", text=msg)]


async def handle_send_break(arguments: dict, *, SendBreakParams) -> list[TextContent]:
    """Send a break/interrupt signal to an active debugger session."""
    args = SendBreakParams(**arguments)
    session_id = os.path.abspath(args.dump_path)
    with _session_lock:
        session = active_sessions.get(session_id)
    if session is None:
        raise McpError(
            ErrorData(code=INVALID_PARAMS, message=f"No active session: {args.dump_path}")
        )
    sent = await asyncio.to_thread(session.send_break)
    if sent:
        msg = f"Break signal sent to {session.backend_name()} session for {args.dump_path}"
    else:
        msg = f"Session process not running: {args.dump_path}"
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
        raise McpError(
            ErrorData(code=INVALID_PARAMS, message="No directory specified and no default found")
        )

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
            size_str = str(round(os.path.getsize(d) / (1024 * 1024), 2))
        except OSError:
            size_str = "?"
        result += f"{i + 1}. {d} ({size_str} MB)\n"

    return [TextContent(type="text", text=result)]
