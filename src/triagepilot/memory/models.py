"""Pydantic models for the persistent memory system."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


class TriageMemoryEntry(BaseModel):
    """A single stored crash triage record."""

    id: str = Field(default_factory=_new_id)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    # Dump metadata
    dump_path: str
    platform: str | None = None
    debugger_type: str | None = None

    # Crash identity
    crash_signature: str
    exception_type: str | None = None
    faulting_module: str | None = None
    faulting_function: str | None = None
    faulting_file: str | None = None
    faulting_line: int | None = None
    stack_hash: str | None = None

    # Triage results (filled in by save_triage_result)
    root_cause: str | None = None
    fix_description: str | None = None
    fix_pr_url: str | None = None

    # Investigation context
    debugger_commands_used: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    # Scoring / lifecycle
    confidence: float = 1.0
    hit_count: int = 0
    last_recalled_at: datetime | None = None

    # Search data
    raw_analysis_snippet: str | None = None
    tokens: list[str] = Field(default_factory=list)

    # Relationships
    related_entries: list[str] = Field(default_factory=list)


class TriageRecallResult(BaseModel):
    """A memory recall result with similarity metadata."""

    entry: TriageMemoryEntry
    similarity_score: float
    match_reasons: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# MCP tool parameter models
# ---------------------------------------------------------------------------


class RecallSimilarParams(BaseModel):
    """Parameters for the recall_similar_crashes MCP tool."""

    analysis_text: str | None = Field(
        default=None,
        description="Raw crash analysis output to search against.",
    )
    crash_signature: str | None = Field(
        default=None,
        description="Normalized crash signature to match.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Tags to filter by.",
    )
    limit: int = Field(
        default=5,
        description="Maximum number of results to return.",
        ge=1,
        le=50,
    )


class SaveTriageParams(BaseModel):
    """Parameters for the save_triage_result MCP tool."""

    dump_path: str = Field(description="Path to the crash dump that was analyzed.")
    root_cause: str | None = Field(
        default=None,
        description="Root cause description.",
    )
    fix_description: str | None = Field(
        default=None,
        description="Description of the fix applied.",
    )
    fix_pr_url: str | None = Field(
        default=None,
        description="URL of the pull request with the fix.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Tags to attach to this triage record.",
    )
    debugger_commands_used: list[str] = Field(
        default_factory=list,
        description="Debugger commands that were useful during investigation.",
    )


class ListPatternsParams(BaseModel):
    """Parameters for the list_known_patterns MCP tool."""

    tag_filter: str | None = Field(
        default=None,
        description="Filter patterns by tag.",
    )
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class ForgetPatternParams(BaseModel):
    """Parameters for the forget_pattern MCP tool."""

    pattern_id: str = Field(description="ID of the memory entry to delete.")
