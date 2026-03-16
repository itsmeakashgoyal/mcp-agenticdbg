"""Crash signature extraction and stack hashing for the memory system.

Reuses regex patterns from triagepilot.tools.debugger_tools to parse
CDB, GDB, and LLDB analysis output into normalized crash fingerprints.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Regex patterns (mirrored from debugger_tools.py for decoupling)
# ---------------------------------------------------------------------------

# CDB: FAULTING_SOURCE_FILE / SYMBOL_NAME / MODULE_NAME
_FAULTING_FILE_RE = re.compile(r"^FAULTING_SOURCE_FILE:\s+(.+)$", re.MULTILINE)
_FAULTING_LINE_RE = re.compile(r"^FAULTING_SOURCE_LINE_NUMBER:\s+(\d+)$", re.MULTILINE)
_SYMBOL_NAME_RE = re.compile(r"^SYMBOL_NAME:\s+(\w+)!([A-Za-z0-9_:~]+)", re.MULTILINE)
_MODULE_NAME_RE = re.compile(r"^MODULE_NAME:\s+(\w+)", re.MULTILINE)
_STACK_FRAME_RE = re.compile(r"(\w+)!([A-Za-z0-9_:~]+)\+0x([0-9a-fA-F]+)")

# CDB exception code
_EXCEPTION_CODE_RE = re.compile(r"^ExceptionCode:\s+(0x[0-9a-fA-F]+)", re.MULTILINE)
_EXCEPTION_NAME_RE = re.compile(
    r"^EXCEPTION_RECORD:.*?ExceptionCode:\s+\w+\s+\(([A-Z_]+)\)", re.MULTILINE
)
_CDB_BUGCHECK_RE = re.compile(r"^BUGCHECK_STR:\s+(.+)$", re.MULTILINE)

# GDB/LLDB
_GDB_AT_RE = re.compile(
    r"\bat\s+([\w./\-\\]+\.(?:c|cpp|cc|cxx|h|hpp|hxx|inl|rs|go|py|m|mm|swift)):(\d+)",
    re.MULTILINE,
)
_GDB_FRAME_FUNC_RE = re.compile(
    r"^#\d+\s+(?:0x[0-9a-fA-F]+\s+in\s+)?([A-Za-z_][A-Za-z0-9_:<>~*]+)\s*\(",
    re.MULTILINE,
)
_GDB_SIGNAL_RE = re.compile(r"Program received signal\s+(\w+)", re.MULTILINE)
_LLDB_SIGNAL_RE = re.compile(r"stop reason\s*=\s*signal\s+(\w+)", re.MULTILINE)
_LLDB_EXC_RE = re.compile(r"stop reason\s*=\s*EXC_(\w+)", re.MULTILINE)

# Skip frames
_SKIP_FUNCTIONS = frozenset(
    {
        "??",
        "__libc_start_main",
        "__GI___libc_start_main",
        "_start",
        "__cxa_throw",
        "__cxa_allocate_exception",
        "abort",
        "raise",
        "__pthread_kill",
    }
)

# Hex literal pattern for token cleaning
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")
_ADDR_RE = re.compile(r"\b[0-9a-fA-F]{8,}\b")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class CrashSignature:
    """Normalized crash fingerprint."""

    exception_type: str | None = None
    faulting_module: str | None = None
    faulting_function: str | None = None
    faulting_file: str | None = None
    faulting_line: int | None = None
    offset_bucket: str | None = None
    raw_signature: str = ""

    def normalized(self) -> str:
        """Return the canonical signature string."""
        parts = [
            self.exception_type or "UNKNOWN",
            self.faulting_module or "unknown",
            self.faulting_function or "unknown",
            self.offset_bucket or "0",
        ]
        return "|".join(parts)


# ---------------------------------------------------------------------------
# Offset bucketing
# ---------------------------------------------------------------------------


def _bucket_offset(offset_hex: str) -> str:
    """Bucket a hex offset into a range for near-miss matching."""
    try:
        val = int(offset_hex, 16)
    except (ValueError, TypeError):
        return "0"
    if val <= 0xFF:
        return "0x0-0xFF"
    if val <= 0xFFF:
        return "0x100-0xFFF"
    if val <= 0xFFFF:
        return "0x1000-0xFFFF"
    return "0x10000+"


# ---------------------------------------------------------------------------
# Extraction functions
# ---------------------------------------------------------------------------


def _extract_exception_type(text: str, debugger_type: str) -> str | None:
    """Extract the exception/signal type from analysis output."""
    if debugger_type == "cdb":
        m = _EXCEPTION_NAME_RE.search(text)
        if m:
            return m.group(1)
        m = _CDB_BUGCHECK_RE.search(text)
        if m:
            return m.group(1).strip()
        m = _EXCEPTION_CODE_RE.search(text)
        if m:
            return m.group(1)
    elif debugger_type == "gdb":
        m = _GDB_SIGNAL_RE.search(text)
        if m:
            return m.group(1)
    elif debugger_type == "lldb":
        m = _LLDB_EXC_RE.search(text)
        if m:
            return f"EXC_{m.group(1)}"
        m = _LLDB_SIGNAL_RE.search(text)
        if m:
            return m.group(1)

    # Fallback: check for common signal names in text
    for sig in ("SIGSEGV", "SIGABRT", "SIGBUS", "SIGFPE", "SIGILL", "SIGTRAP"):
        if sig in text:
            return sig
    return None


def _extract_cdb_identity(text: str) -> tuple[str | None, str | None, str | None]:
    """Extract module, function, offset from CDB analysis."""
    sym = _SYMBOL_NAME_RE.search(text)
    mod = _MODULE_NAME_RE.search(text)

    module = mod.group(1) if mod else None
    function = None
    offset = None

    if sym:
        module = module or sym.group(1)
        raw_func = sym.group(2)
        function = raw_func.rsplit("::", 1)[-1] if "::" in raw_func else raw_func

    # Try to get offset from first stack frame matching the faulting module
    if module:
        for fm, fs, fo in _STACK_FRAME_RE.findall(text):
            if fm.lower() == module.lower():
                offset = fo
                if not function:
                    function = fs.rsplit("::", 1)[-1] if "::" in fs else fs
                break

    return module, function, offset


def _extract_gdb_lldb_identity(
    text: str,
) -> tuple[str | None, str | None, str | None, str | None, int | None]:
    """Extract module, function, file, line from GDB/LLDB analysis."""
    # File/line from "at file:line"
    at_matches = _GDB_AT_RE.findall(text)
    faulting_file = at_matches[0][0] if at_matches else None
    faulting_line = int(at_matches[0][1]) if at_matches else None

    # Function from frame
    functions = []
    for m in _GDB_FRAME_FUNC_RE.finditer(text):
        raw = m.group(1)
        bare = raw.rsplit("::", 1)[-1] if "::" in raw else raw
        if bare not in _SKIP_FUNCTIONS and not bare.startswith("__"):
            functions.append(bare)
            break

    function = functions[0] if functions else None

    # CDB-style frames might also be present (mixed output)
    module = None
    offset = None
    cdb_frames = _STACK_FRAME_RE.findall(text)
    if cdb_frames:
        module = cdb_frames[0][0]
        offset = cdb_frames[0][2]

    return module, function, faulting_file, faulting_line, offset


def extract_crash_signature(analysis_text: str, debugger_type: str = "auto") -> CrashSignature:
    """Extract a normalized crash signature from analysis output.

    Parameters
    ----------
    analysis_text:
        Full text output from a crash analysis session.
    debugger_type:
        One of "cdb", "gdb", "lldb", or "auto" (tries all parsers).

    Returns
    -------
    CrashSignature with normalized fields populated.
    """
    sig = CrashSignature()

    # Auto-detect debugger type from output if needed
    if debugger_type == "auto":
        if _SYMBOL_NAME_RE.search(analysis_text) or _MODULE_NAME_RE.search(analysis_text):
            debugger_type = "cdb"
        elif _GDB_SIGNAL_RE.search(analysis_text) or "GNU gdb" in analysis_text:
            debugger_type = "gdb"
        elif _LLDB_SIGNAL_RE.search(analysis_text) or "lldb" in analysis_text.lower():
            debugger_type = "lldb"
        else:
            debugger_type = "gdb"  # fallback

    sig.exception_type = _extract_exception_type(analysis_text, debugger_type)

    if debugger_type == "cdb":
        module, function, offset = _extract_cdb_identity(analysis_text)
        sig.faulting_module = module
        sig.faulting_function = function
        sig.offset_bucket = _bucket_offset(offset) if offset else None

        # CDB faulting source info
        file_m = _FAULTING_FILE_RE.search(analysis_text)
        line_m = _FAULTING_LINE_RE.search(analysis_text)
        sig.faulting_file = file_m.group(1).strip() if file_m else None
        sig.faulting_line = int(line_m.group(1)) if line_m else None
    else:
        module, function, f_file, f_line, offset = _extract_gdb_lldb_identity(analysis_text)
        sig.faulting_module = module
        sig.faulting_function = function
        sig.faulting_file = f_file
        sig.faulting_line = f_line
        sig.offset_bucket = _bucket_offset(offset) if offset else None

    sig.raw_signature = sig.normalized()
    return sig


# ---------------------------------------------------------------------------
# Stack hashing
# ---------------------------------------------------------------------------


def _extract_normalized_frames(analysis_text: str, top_n: int = 5) -> list[str]:
    """Extract top N normalized stack frames for hashing."""
    frames: list[str] = []

    # CDB-style: module!Function+0xOffset
    for module, func, _offset in _STACK_FRAME_RE.findall(analysis_text):
        bare = func.rsplit("::", 1)[-1] if "::" in func else func
        if bare not in _SKIP_FUNCTIONS:
            frames.append(f"{module.lower()}!{bare}")
        if len(frames) >= top_n:
            return frames

    # GDB/LLDB-style: #N in func_name
    if len(frames) < top_n:
        for m in _GDB_FRAME_FUNC_RE.finditer(analysis_text):
            raw = m.group(1)
            bare = raw.rsplit("::", 1)[-1] if "::" in raw else raw
            if bare not in _SKIP_FUNCTIONS and not bare.startswith("__"):
                frames.append(bare)
            if len(frames) >= top_n:
                return frames

    return frames


def compute_stack_hash(analysis_text: str, top_n: int = 5) -> str | None:
    """Compute a SHA256 hash of the top N normalized stack frames.

    Returns None if no frames could be extracted.
    """
    frames = _extract_normalized_frames(analysis_text, top_n)
    if not frames:
        return None
    content = "\n".join(frames)
    return hashlib.sha256(content.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Tokenization for TF-IDF search
# ---------------------------------------------------------------------------


def tokenize_for_search(
    analysis_text: str,
    faulting_file: str | None = None,
    tags: list[str] | None = None,
) -> list[str]:
    """Produce a list of search tokens from analysis text and metadata.

    Tokens are lowercased, with hex addresses removed. Duplicates are preserved
    (TF needs frequency), but very common stopwords are excluded.
    """
    _STOPWORDS = frozenset(
        {
            "the", "a", "an", "is", "at", "in", "on", "of", "to", "for",
            "and", "or", "not", "with", "from", "by", "as", "this", "that",
            "it", "be", "are", "was", "were", "been", "has", "have", "had",
            "no", "do", "does", "did", "will", "can", "could", "should",
        }
    )

    text = analysis_text
    # Remove hex addresses
    text = _HEX_RE.sub(" ", text)
    text = _ADDR_RE.sub(" ", text)
    # Tokenize
    raw_tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text)
    tokens = [t.lower() for t in raw_tokens if t.lower() not in _STOPWORDS]

    # Add file path components
    if faulting_file:
        parts = re.split(r"[/\\.]", faulting_file)
        tokens.extend(p.lower() for p in parts if len(p) > 2)

    # Add tags
    if tags:
        tokens.extend(t.lower() for t in tags)

    return tokens


# ---------------------------------------------------------------------------
# Auto-tagging
# ---------------------------------------------------------------------------


def extract_auto_tags(
    analysis_text: str,
    debugger_type: str | None = None,
    faulting_file: str | None = None,
    exception_type: str | None = None,
    faulting_module: str | None = None,
) -> list[str]:
    """Extract tags automatically from analysis output."""
    tags: list[str] = []

    if exception_type:
        tags.append(exception_type.lower())

    if faulting_module:
        tags.append(f"module:{faulting_module.lower()}")

    if debugger_type:
        tags.append(f"debugger:{debugger_type}")

    # Language from file extension
    if faulting_file:
        ext = faulting_file.rsplit(".", 1)[-1].lower() if "." in faulting_file else None
        lang_map = {
            "c": "c", "cpp": "c++", "cc": "c++", "cxx": "c++",
            "h": "c/c++", "hpp": "c++", "hxx": "c++",
            "rs": "rust", "go": "go", "swift": "swift",
            "m": "objc", "mm": "objc++", "py": "python",
        }
        if ext and ext in lang_map:
            tags.append(f"lang:{lang_map[ext]}")

    # Crash category heuristics
    text_lower = analysis_text.lower()
    if any(k in text_lower for k in ("null pointer", "null deref", "nullptr", "nil pointer")):
        tags.append("null-deref")
    if any(k in text_lower for k in ("use after free", "use-after-free", "heap-use-after-free")):
        tags.append("use-after-free")
    if any(k in text_lower for k in ("stack overflow", "stack_overflow")):
        tags.append("stack-overflow")
    if any(k in text_lower for k in ("heap corruption", "heap_corruption")):
        tags.append("heap-corruption")
    if any(k in text_lower for k in ("deadlock", "lock order")):
        tags.append("deadlock")
    if any(k in text_lower for k in ("buffer overflow", "buffer overrun")):
        tags.append("buffer-overflow")
    if "assertion" in text_lower or "assert" in text_lower:
        tags.append("assertion")

    return sorted(set(tags))
