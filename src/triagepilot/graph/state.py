"""Typed state schema for the crash analysis LangGraph."""

from __future__ import annotations

from typing import Literal, Optional, TypedDict


class CrashAnalysisState(TypedDict, total=False):
    """State carried through the crash-analysis graph.

    All keys except ``dump_path`` are optional so that nodes can
    incrementally populate the state as they execute.
    """

    # --- Inputs (set at graph invocation) ---
    dump_path: str
    symbols_path: Optional[str]
    image_path: Optional[str]
    repo_path: Optional[str]
    jira_id: Optional[str]

    # --- Debugger analysis results ---
    crash_info: Optional[str]
    analyze_output: Optional[str]
    stack_trace: Optional[str]
    modules: Optional[str]
    threads: Optional[str]
    faulting_source: Optional[str]
    metadata: Optional[dict]

    # --- LLM-generated outputs ---
    root_cause: Optional[str]
    suggested_fixes: Optional[list[dict]]
    report: Optional[str]

    # --- Routing decisions ---
    change_type: Optional[Literal["shared", "repo", "mixed", "none"]]
    pr_url: Optional[str]
    patch_path: Optional[str]

    # --- Control flow ---
    retry_count: int
    errors: list[str]
    status: Literal["analyzing", "diagnosing", "fixing", "reporting", "done", "error"]

    # --- Server context (injected once, read-only) ---
    debugger_path: Optional[str]
    debugger_type: str
    timeout: int
    verbose: bool
    max_retries: int
    llm_provider: Optional[str]
    llm_model: Optional[str]
    llm_api_key: Optional[str]
