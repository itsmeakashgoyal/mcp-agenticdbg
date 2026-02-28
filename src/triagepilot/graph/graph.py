"""Build and compile the crash analysis LangGraph."""

from __future__ import annotations

import logging
import os
from typing import Any

from langgraph.graph import StateGraph, END

from .state import CrashAnalysisState
from .nodes import (
    analyze_dump_node,
    extract_metadata_node,
    source_lookup_node,
    root_cause_node,
    suggest_fix_node,
    classify_changes_node,
    create_pr_node,
    shared_patch_node,
    summary_node,
)
from .edges import should_retry_analyze, route_changes

logger = logging.getLogger(__name__)


def _configure_langsmith() -> None:
    """Enable LangSmith tracing when ``TRIAGEPILOT_LANGSMITH_API_KEY`` is set.

    LangSmith looks for the standard ``LANGCHAIN_*`` env vars.  This
    function bridges from the ``TRIAGEPILOT_`` namespace so users only need
    to configure our prefix.
    """
    api_key = os.environ.get("TRIAGEPILOT_LANGSMITH_API_KEY")
    if not api_key:
        return

    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGCHAIN_API_KEY", api_key)
    os.environ.setdefault(
        "LANGCHAIN_PROJECT",
        os.environ.get("TRIAGEPILOT_LANGSMITH_PROJECT", "triagepilot"),
    )
    logger.info("LangSmith tracing enabled (project=%s)", os.environ.get("LANGCHAIN_PROJECT"))


def build_crash_analysis_graph(*, include_llm_nodes: bool = True):
    """Construct and compile the crash-analysis state graph.

    When *include_llm_nodes* is ``False`` the LLM-based nodes
    (root cause, suggest fix) are skipped and the graph goes straight
    from source lookup to change classification.  This is useful for
    environments where no LLM API key is configured.
    """
    _configure_langsmith()

    builder = StateGraph(CrashAnalysisState)

    # --- Nodes ---
    builder.add_node("analyze_dump", analyze_dump_node)
    builder.add_node("extract_metadata", extract_metadata_node)
    builder.add_node("source_lookup", source_lookup_node)
    builder.add_node("classify_changes", classify_changes_node)
    builder.add_node("create_pr", create_pr_node)
    builder.add_node("shared_patch", shared_patch_node)
    builder.add_node("summary", summary_node)

    if include_llm_nodes:
        builder.add_node("root_cause", root_cause_node)
        builder.add_node("suggest_fix", suggest_fix_node)

    # --- Edges ---

    # START -> analyze_dump
    builder.set_entry_point("analyze_dump")

    # analyze_dump -> retry check
    builder.add_conditional_edges(
        "analyze_dump",
        should_retry_analyze,
        {
            "retry": "analyze_dump",
            "continue": "extract_metadata",
        },
    )

    # extract_metadata -> source_lookup
    builder.add_edge("extract_metadata", "source_lookup")

    if include_llm_nodes:
        # source_lookup -> root_cause -> suggest_fix -> classify_changes
        builder.add_edge("source_lookup", "root_cause")
        builder.add_edge("root_cause", "suggest_fix")
        builder.add_edge("suggest_fix", "classify_changes")
    else:
        # Skip LLM nodes
        builder.add_edge("source_lookup", "classify_changes")

    # classify_changes -> route to output node
    builder.add_conditional_edges(
        "classify_changes",
        route_changes,
        {
            "shared": "shared_patch",
            "repo": "create_pr",
            "mixed": "shared_patch",
            "none": "summary",
        },
    )

    # Output nodes -> summary
    builder.add_edge("create_pr", "summary")
    builder.add_edge("shared_patch", "summary")

    # summary -> END
    builder.add_edge("summary", END)

    return builder.compile()
