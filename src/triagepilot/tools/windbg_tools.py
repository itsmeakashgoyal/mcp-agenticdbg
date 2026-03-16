"""Backward-compatible re-export of debugger tools.

All functionality has moved to ``debugger_tools.py``.  This module
re-exports everything so that existing imports continue to work.
"""

from .debugger_tools import (  # noqa: F401
    _FAULTING_FILE_RE,
    _FAULTING_LINE_RE,
    _MODULE_NAME_RE,
    _SOURCE_CONTEXT_LINES,
    _SOURCE_EXTENSIONS,
    _SOURCE_LOOKUP_MAX_FILES,
    _SOURCE_LOOKUP_MAX_SECONDS,
    _STACK_FRAME_RE,
    _SYMBOL_NAME_RE,
    BLOCKED_COMMAND_PREFIXES_CDB,
    _best_match,
    _cmd_rate_limiter,
    _extract_stack_functions,
    _find_file_in_repo,
    _find_function_in_repo,
    _format_function_matches,
    _parse_faulting_module_function,
    _parse_faulting_source,
    _read_source_context,
    _TokenBucket,
    active_session_count,
    active_sessions,
    cleanup_all_sessions,
    close_session,
    get_local_dumps_path,
    get_or_create_session,
    handle_analyze_dump,
    handle_close_dump,
    handle_list_dumps,
    handle_open_dump,
    handle_run_cmd,
    locate_faulting_source,
    set_max_concurrent_sessions,
    validate_cdb_command,
    validate_debugger_command,
)

# Alias for backward compatibility
BLOCKED_COMMAND_PREFIXES = BLOCKED_COMMAND_PREFIXES_CDB
