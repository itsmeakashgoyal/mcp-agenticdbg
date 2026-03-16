"""Typed state schema for the crash analysis LangGraph."""

from __future__ import annotations

from typing import Literal, TypedDict


class CrashAnalysisState(TypedDict, total=False):
    """State carried through the crash-analysis graph.

    All keys except ``dump_path`` are optional so that nodes can
    incrementally populate the state as they execute.
    """

    # --- Inputs (set at graph invocation) ---
    dump_path: str
    symbols_path: str | None
    image_path: str | None
    repo_path: str | None
    jira_id: str | None

    # --- Debugger analysis results ---
    crash_info: str | None
    analyze_output: str | None
    stack_trace: str | None
    modules: str | None
    threads: str | None
    faulting_source: str | None
    metadata: dict | None

    # --- LLM-generated outputs ---
    root_cause: str | None
    suggested_fixes: list[dict] | None
    report: str | None

    # --- Routing decisions ---
    change_type: Literal["shared", "repo", "mixed", "none"] | None
    pr_url: str | None
    patch_path: str | None

    # --- Control flow ---
    retry_count: int
    errors: list[str]
    status: Literal["analyzing", "diagnosing", "fixing", "reporting", "done", "error"]

    # --- Memory system ---
    similar_cases: list[dict] | None
    memory_db_path: str | None

    # --- Server context (injected once, read-only) ---
    debugger_path: str | None
    debugger_type: str
    timeout: int
    verbose: bool
    max_retries: int
    llm_provider: str | None
    llm_model: str | None
    llm_api_key: str | None
