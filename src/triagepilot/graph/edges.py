"""Conditional edge functions for the crash analysis LangGraph."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def should_retry_analyze(state: dict) -> str:
    """Decide whether to retry the analyze node.

    Returns ``"retry"`` when the analysis output is missing and retries
    have not been exhausted, otherwise ``"continue"``.
    """
    max_retries = state.get("max_retries", 3)
    retry_count = state.get("retry_count", 0)

    if not state.get("analyze_output") and retry_count < max_retries:
        logger.info("Retrying analysis (attempt %d/%d)", retry_count + 1, max_retries)
        return "retry"
    return "continue"


def route_changes(state: dict) -> str:
    """Route to the appropriate output node based on ``change_type``.

    Returns one of ``"shared"``, ``"repo"``, ``"mixed"``, or ``"none"``.
    """
    change_type = state.get("change_type", "none")
    logger.info("Routing changes: %s", change_type)
    return change_type


def has_source(state: dict) -> str:
    """Check whether faulting source was found.

    Returns ``"found"`` or ``"not_found"``.
    """
    if state.get("faulting_source"):
        return "found"
    return "not_found"
