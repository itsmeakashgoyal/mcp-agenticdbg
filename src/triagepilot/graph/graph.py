"""Build and compile the crash analysis LangGraph."""

from __future__ import annotations

import logging
import os

from langgraph.graph import END, StateGraph

from .edges import route_changes, should_retry_analyze
from .nodes import (
    analyze_dump_node,
    classify_changes_node,
    create_pr_node,
    extract_metadata_node,
    memory_recall_node,
    memory_save_node,
    root_cause_node,
    shared_patch_node,
    source_lookup_node,
    suggest_fix_node,
    summary_node,
)
from .state import CrashAnalysisState

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
    builder.add_node("memory_recall", memory_recall_node)
    builder.add_node("classify_changes", classify_changes_node)
    builder.add_node("create_pr", create_pr_node)
    builder.add_node("shared_patch", shared_patch_node)
    builder.add_node("summary", summary_node)
    builder.add_node("memory_save", memory_save_node)

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

    # extract_metadata -> source_lookup -> memory_recall
    builder.add_edge("extract_metadata", "source_lookup")
    builder.add_edge("source_lookup", "memory_recall")

    if include_llm_nodes:
        # memory_recall -> root_cause -> suggest_fix -> classify_changes
        builder.add_edge("memory_recall", "root_cause")
        builder.add_edge("root_cause", "suggest_fix")
        builder.add_edge("suggest_fix", "classify_changes")
    else:
        # Skip LLM nodes
        builder.add_edge("memory_recall", "classify_changes")

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

    # Output nodes -> summary -> memory_save -> END
    builder.add_edge("create_pr", "summary")
    builder.add_edge("shared_patch", "summary")
    builder.add_edge("summary", "memory_save")
    builder.add_edge("memory_save", END)

    return builder.compile()
