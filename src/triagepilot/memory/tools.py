"""MCP tool handlers for the persistent memory system."""

from __future__ import annotations

import logging

from mcp.types import TextContent

from .models import (
    ForgetPatternParams,
    ListPatternsParams,
    RecallSimilarParams,
    SaveTriageParams,
    TriageMemoryEntry,
)
from .signature import (
    compute_stack_hash,
    extract_auto_tags,
    extract_crash_signature,
    tokenize_for_search,
)
from .store import MemoryStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_entry_summary(entry: TriageMemoryEntry, score: float | None = None) -> str:
    """Format a single memory entry as a markdown summary."""
    lines: list[str] = []

    header = f"**{entry.crash_signature}**"
    if score is not None:
        header += f" (score: {score:.0%})"
    lines.append(header)

    lines.append(f"- **ID:** `{entry.id}`")
    if entry.exception_type:
        lines.append(f"- **Exception:** {entry.exception_type}")
    if entry.faulting_module:
        lines.append(f"- **Module:** {entry.faulting_module}")
    if entry.faulting_function:
        lines.append(f"- **Function:** {entry.faulting_function}")
    if entry.faulting_file:
        loc = entry.faulting_file
        if entry.faulting_line:
            loc += f":{entry.faulting_line}"
        lines.append(f"- **Location:** {loc}")
    if entry.root_cause:
        lines.append(f"- **Root Cause:** {entry.root_cause}")
    if entry.fix_description:
        lines.append(f"- **Fix:** {entry.fix_description}")
    if entry.fix_pr_url:
        lines.append(f"- **PR:** {entry.fix_pr_url}")
    if entry.tags:
        lines.append(f"- **Tags:** {', '.join(entry.tags)}")
    if entry.debugger_commands_used:
        lines.append(f"- **Useful Commands:** `{'`, `'.join(entry.debugger_commands_used)}`")
    lines.append(
        f"- **Confidence:** {entry.confidence:.0%} | "
        f"**Hits:** {entry.hit_count} | "
        f"**Last seen:** {entry.updated_at.strftime('%Y-%m-%d')}"
    )

    return "\n".join(lines)


def format_recall_results(
    results: list[tuple[TriageMemoryEntry, float, list[str]]],
) -> str:
    """Format recall results as a markdown section for prepending to analysis."""
    if not results:
        return ""

    lines = [
        "### Similar Past Crashes (from memory)\n",
        f"Found **{len(results)}** similar past triage(s):\n",
    ]

    for i, (entry, score, reasons) in enumerate(results, 1):
        lines.append(f"#### Match {i}")
        if reasons:
            lines.append(f"*Match reason: {'; '.join(reasons)}*\n")
        lines.append(_format_entry_summary(entry, score))
        # Command playbook
        if entry.debugger_commands_used:
            lines.append("\n**Recommended investigation commands:**")
            for cmd in entry.debugger_commands_used[:5]:
                lines.append(f"- `{cmd}`")
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def handle_recall_similar(
    args: RecallSimilarParams, memory_store: MemoryStore
) -> list[TextContent]:
    """Handle the recall_similar_crashes MCP tool."""
    query_sig: str | None = args.crash_signature
    query_tokens: list[str] | None = None
    query_stack_hash: str | None = None

    # If analysis text provided, extract signature and tokens
    if args.analysis_text:
        sig = extract_crash_signature(args.analysis_text)
        if not query_sig:
            query_sig = sig.normalized()
        query_stack_hash = compute_stack_hash(args.analysis_text)
        query_tokens = tokenize_for_search(args.analysis_text, sig.faulting_file, args.tags)
    elif args.tags:
        query_tokens = [t.lower() for t in args.tags]

    results = memory_store.recall(
        query_signature=query_sig,
        query_stack_hash=query_stack_hash,
        query_tokens=query_tokens,
        limit=args.limit,
    )

    if not results:
        return [TextContent(type="text", text="No similar crashes found in memory.\n")]

    formatted = format_recall_results(
        [(r.entry, r.similarity_score, r.match_reasons) for r in results]
    )
    return [TextContent(type="text", text=formatted)]


async def handle_save_triage(
    args: SaveTriageParams, memory_store: MemoryStore
) -> list[TextContent]:
    """Handle the save_triage_result MCP tool."""
    # Check if there's an existing entry for this dump
    existing = memory_store.get_by_dump_path(args.dump_path)

    if existing:
        # Update existing entry with triage results
        updates: dict[str, object] = {}
        if args.root_cause:
            updates["root_cause"] = args.root_cause
        if args.fix_description:
            updates["fix_description"] = args.fix_description
        if args.fix_pr_url:
            updates["fix_pr_url"] = args.fix_pr_url
        if args.tags:
            merged_tags = sorted(set(existing.tags) | set(args.tags))
            updates["tags"] = merged_tags
        if args.debugger_commands_used:
            merged_cmds = list(
                dict.fromkeys(existing.debugger_commands_used + args.debugger_commands_used)
            )
            updates["debugger_commands_used"] = merged_cmds

        # Boost confidence on fix confirmation
        if args.fix_pr_url or args.fix_description:
            updates["confidence"] = min(1.0, existing.confidence + 0.2)

        memory_store.update_entry(existing.id, **updates)
        return [
            TextContent(
                type="text",
                text=f"Updated triage memory `{existing.id}` for `{args.dump_path}`.\n"
                f"Signature: `{existing.crash_signature}`\n",
            )
        ]

    # No existing entry — create a minimal one
    entry = TriageMemoryEntry(
        dump_path=args.dump_path,
        crash_signature="manual|unknown|unknown|0",
        root_cause=args.root_cause,
        fix_description=args.fix_description,
        fix_pr_url=args.fix_pr_url,
        tags=args.tags,
        debugger_commands_used=args.debugger_commands_used,
    )
    entry_id = memory_store.save(entry)
    return [
        TextContent(
            type="text",
            text=f"Saved new triage memory `{entry_id}` for `{args.dump_path}`.\n",
        )
    ]


async def handle_list_patterns(
    args: ListPatternsParams, memory_store: MemoryStore
) -> list[TextContent]:
    """Handle the list_known_patterns MCP tool."""
    entries = memory_store.list_patterns(
        offset=args.offset, limit=args.limit, tag_filter=args.tag_filter
    )

    if not entries:
        return [TextContent(type="text", text="No crash patterns stored in memory.\n")]

    stats = memory_store.stats()
    lines = [
        f"### Known Crash Patterns ({stats['total_entries']} total)\n",
    ]

    for i, entry in enumerate(entries, args.offset + 1):
        lines.append(f"**{i}.** {_format_entry_summary(entry)}\n")

    if stats.get("top_tags"):
        lines.append(
            "**Top tags:** "
            + ", ".join(f"`{tag}` ({count})" for tag, count in stats["top_tags"].items())
        )

    return [TextContent(type="text", text="\n".join(lines) + "\n")]


async def handle_forget_pattern(
    args: ForgetPatternParams, memory_store: MemoryStore
) -> list[TextContent]:
    """Handle the forget_pattern MCP tool."""
    deleted = memory_store.forget(args.pattern_id)
    if deleted:
        return [
            TextContent(
                type="text",
                text=f"Deleted memory entry `{args.pattern_id}`.\n",
            )
        ]
    return [
        TextContent(
            type="text",
            text=f"No memory entry found with ID `{args.pattern_id}`.\n",
        )
    ]


# ---------------------------------------------------------------------------
# Auto-save/recall helpers (called from debugger_tools._run_dump_analysis)
# ---------------------------------------------------------------------------


def auto_save_analysis(
    memory_store: MemoryStore,
    dump_path: str,
    analysis_text: str,
    debugger_type: str = "auto",
    platform: str | None = None,
) -> str | None:
    """Auto-save a crash analysis to memory after analyze_dump completes.

    Returns the entry ID if saved, None on error.
    """
    try:
        sig = extract_crash_signature(analysis_text, debugger_type)
        stack_hash = compute_stack_hash(analysis_text)
        tokens = tokenize_for_search(analysis_text, sig.faulting_file)
        auto_tags = extract_auto_tags(
            analysis_text,
            debugger_type=debugger_type,
            faulting_file=sig.faulting_file,
            exception_type=sig.exception_type,
            faulting_module=sig.faulting_module,
        )

        entry = TriageMemoryEntry(
            dump_path=dump_path,
            platform=platform,
            debugger_type=debugger_type if debugger_type != "auto" else None,
            crash_signature=sig.normalized(),
            exception_type=sig.exception_type,
            faulting_module=sig.faulting_module,
            faulting_function=sig.faulting_function,
            faulting_file=sig.faulting_file,
            faulting_line=sig.faulting_line,
            stack_hash=stack_hash,
            tags=auto_tags,
            raw_analysis_snippet=analysis_text[:2000],
            tokens=tokens,
        )
        return memory_store.save(entry)
    except Exception:
        logger.warning("Memory auto-save failed", exc_info=True)
        return None


def auto_recall_similar(
    memory_store: MemoryStore,
    analysis_text: str,
    limit: int = 3,
) -> str:
    """Auto-recall similar crashes and return formatted markdown section.

    Returns empty string if no matches found.
    """
    try:
        sig = extract_crash_signature(analysis_text)
        stack_hash = compute_stack_hash(analysis_text)
        tokens = tokenize_for_search(analysis_text, sig.faulting_file)

        results = memory_store.recall(
            query_signature=sig.normalized(),
            query_stack_hash=stack_hash,
            query_tokens=tokens,
            limit=limit,
        )
        if not results:
            return ""

        return format_recall_results(
            [(r.entry, r.similarity_score, r.match_reasons) for r in results]
        )
    except Exception:
        logger.warning("Memory auto-recall failed", exc_info=True)
        return ""
