"""SQLite-backed persistent memory store for crash triage knowledge."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .models import TriageMemoryEntry, TriageRecallResult
from .similarity import (
    compute_overall_score,
    score_signature_match,
    score_stack_hash_match,
    score_tfidf_similarity,
)

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1

_CREATE_TABLES = """\
CREATE TABLE IF NOT EXISTS triage_memory (
    id                      TEXT PRIMARY KEY,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    dump_path               TEXT NOT NULL,
    platform                TEXT,
    debugger_type           TEXT,
    crash_signature         TEXT NOT NULL,
    exception_type          TEXT,
    faulting_module         TEXT,
    faulting_function       TEXT,
    faulting_file           TEXT,
    faulting_line           INTEGER,
    stack_hash              TEXT,
    root_cause              TEXT,
    fix_description         TEXT,
    fix_pr_url              TEXT,
    debugger_commands_used  TEXT,
    tags                    TEXT,
    confidence              REAL NOT NULL DEFAULT 1.0,
    hit_count               INTEGER NOT NULL DEFAULT 0,
    last_recalled_at        TEXT,
    raw_analysis_snippet    TEXT,
    tokens                  TEXT,
    related_entries         TEXT
);

CREATE INDEX IF NOT EXISTS idx_crash_signature ON triage_memory(crash_signature);
CREATE INDEX IF NOT EXISTS idx_stack_hash ON triage_memory(stack_hash);
CREATE INDEX IF NOT EXISTS idx_faulting_module ON triage_memory(faulting_module);
CREATE INDEX IF NOT EXISTS idx_faulting_function ON triage_memory(faulting_function);
CREATE INDEX IF NOT EXISTS idx_exception_type ON triage_memory(exception_type);
CREATE INDEX IF NOT EXISTS idx_confidence ON triage_memory(confidence);
CREATE INDEX IF NOT EXISTS idx_created_at ON triage_memory(created_at);

CREATE TABLE IF NOT EXISTS df_table (
    token       TEXT PRIMARY KEY,
    doc_count   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
"""


def _default_db_path() -> str:
    """Return the default database path: ~/.triagepilot/memory.db"""
    home = Path.home() / ".triagepilot"
    home.mkdir(parents=True, exist_ok=True)
    return str(home / "memory.db")


def _entry_to_row(entry: TriageMemoryEntry) -> dict:
    """Convert a TriageMemoryEntry to a flat dict for SQLite insertion."""
    return {
        "id": entry.id,
        "created_at": entry.created_at.isoformat(),
        "updated_at": entry.updated_at.isoformat(),
        "dump_path": entry.dump_path,
        "platform": entry.platform,
        "debugger_type": entry.debugger_type,
        "crash_signature": entry.crash_signature,
        "exception_type": entry.exception_type,
        "faulting_module": entry.faulting_module,
        "faulting_function": entry.faulting_function,
        "faulting_file": entry.faulting_file,
        "faulting_line": entry.faulting_line,
        "stack_hash": entry.stack_hash,
        "root_cause": entry.root_cause,
        "fix_description": entry.fix_description,
        "fix_pr_url": entry.fix_pr_url,
        "debugger_commands_used": json.dumps(entry.debugger_commands_used),
        "tags": json.dumps(entry.tags),
        "confidence": entry.confidence,
        "hit_count": entry.hit_count,
        "last_recalled_at": entry.last_recalled_at.isoformat() if entry.last_recalled_at else None,
        "raw_analysis_snippet": entry.raw_analysis_snippet,
        "tokens": json.dumps(entry.tokens),
        "related_entries": json.dumps(entry.related_entries),
    }


def _row_to_entry(row: sqlite3.Row) -> TriageMemoryEntry:
    """Convert a SQLite row to a TriageMemoryEntry."""
    d = dict(row)
    d["debugger_commands_used"] = json.loads(d["debugger_commands_used"] or "[]")
    d["tags"] = json.loads(d["tags"] or "[]")
    d["tokens"] = json.loads(d["tokens"] or "[]")
    d["related_entries"] = json.loads(d["related_entries"] or "[]")
    d["created_at"] = datetime.fromisoformat(d["created_at"])
    d["updated_at"] = datetime.fromisoformat(d["updated_at"])
    if d["last_recalled_at"]:
        d["last_recalled_at"] = datetime.fromisoformat(d["last_recalled_at"])
    return TriageMemoryEntry(**d)


class MemoryStore:
    """Thread-safe SQLite-backed memory store for crash triage knowledge.

    Usage::

        store = MemoryStore()  # uses default ~/.triagepilot/memory.db
        store.save(entry)
        results = store.recall(query_signature, query_tokens=tokens)
        store.close()
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or _default_db_path()
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._ensure_db()

    def _ensure_db(self) -> None:
        """Open the database and create tables if needed."""
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_CREATE_TABLES)

        # Check/set schema version
        cursor = self._conn.execute("SELECT COUNT(*) FROM schema_version")
        if cursor.fetchone()[0] == 0:
            self._conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,)
            )
        self._conn.commit()

    @property
    def db_path(self) -> str:
        return self._db_path

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self, entry: TriageMemoryEntry) -> str:
        """Save or update a triage memory entry.

        Upserts based on crash_signature + stack_hash match: if an existing
        entry has the same signature and stack hash, it is updated rather
        than creating a duplicate.

        Returns the entry ID.
        """
        with self._lock:
            assert self._conn is not None
            # Check for existing entry with same signature + stack hash
            existing = self._find_existing(entry.crash_signature, entry.stack_hash)
            if existing:
                self._update_existing(existing, entry)
                return existing.id

            row = _entry_to_row(entry)
            cols = ", ".join(row.keys())
            placeholders = ", ".join(f":{k}" for k in row)
            self._conn.execute(f"INSERT INTO triage_memory ({cols}) VALUES ({placeholders})", row)
            # Update document frequencies for TF-IDF
            self._update_df(entry.tokens, increment=True)
            self._conn.commit()
            logger.info("Saved new triage memory: %s (sig=%s)", entry.id, entry.crash_signature)
            return entry.id

    def _find_existing(
        self, crash_signature: str, stack_hash: str | None
    ) -> TriageMemoryEntry | None:
        """Find an existing entry matching signature + stack hash."""
        assert self._conn is not None
        if stack_hash:
            cursor = self._conn.execute(
                "SELECT * FROM triage_memory WHERE crash_signature = ? AND stack_hash = ?",
                (crash_signature, stack_hash),
            )
        else:
            cursor = self._conn.execute(
                "SELECT * FROM triage_memory WHERE crash_signature = ? AND stack_hash IS NULL",
                (crash_signature,),
            )
        row = cursor.fetchone()
        return _row_to_entry(row) if row else None

    def _update_existing(self, existing: TriageMemoryEntry, new: TriageMemoryEntry) -> None:
        """Merge new data into an existing entry."""
        assert self._conn is not None
        now = datetime.now(timezone.utc).isoformat()

        # Merge fields: prefer new non-None values
        updates: dict[str, object] = {"updated_at": now}
        if new.root_cause and not existing.root_cause:
            updates["root_cause"] = new.root_cause
        if new.fix_description and not existing.fix_description:
            updates["fix_description"] = new.fix_description
        if new.fix_pr_url and not existing.fix_pr_url:
            updates["fix_pr_url"] = new.fix_pr_url
        if new.faulting_file and not existing.faulting_file:
            updates["faulting_file"] = new.faulting_file
        if new.faulting_line and not existing.faulting_line:
            updates["faulting_line"] = new.faulting_line

        # Merge tags
        merged_tags = sorted(set(existing.tags) | set(new.tags))
        updates["tags"] = json.dumps(merged_tags)

        # Merge commands
        merged_cmds = list(
            dict.fromkeys(existing.debugger_commands_used + new.debugger_commands_used)
        )
        updates["debugger_commands_used"] = json.dumps(merged_cmds)

        # Update tokens if new ones provided
        if new.tokens:
            # Remove old DF, add new
            self._update_df(existing.tokens, increment=False)
            self._update_df(new.tokens, increment=True)
            updates["tokens"] = json.dumps(new.tokens)

        # Boost confidence on re-observation
        updates["confidence"] = min(1.0, existing.confidence + 0.1)
        updates["hit_count"] = existing.hit_count + 1

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [existing.id]
        self._conn.execute(f"UPDATE triage_memory SET {set_clause} WHERE id = ?", values)
        self._conn.commit()
        logger.info("Updated existing triage memory: %s", existing.id)

    # ------------------------------------------------------------------
    # Recall (similarity search)
    # ------------------------------------------------------------------

    def recall(
        self,
        query_signature: str | None = None,
        query_stack_hash: str | None = None,
        query_tokens: list[str] | None = None,
        limit: int = 5,
    ) -> list[TriageRecallResult]:
        """Retrieve similar past crash triage entries.

        Uses a three-tier scoring system:
        1. Crash signature match (weight 0.5)
        2. Stack hash match (weight 0.3)
        3. TF-IDF keyword similarity (weight 0.2)

        Results are sorted by overall score descending.
        """
        with self._lock:
            assert self._conn is not None
            candidates = self._get_recall_candidates(
                query_signature, query_stack_hash, query_tokens
            )
            if not candidates:
                return []

            # Load DF table for TF-IDF
            df_lookup = self._load_df_lookup()
            total_docs = self._count_entries_unlocked()

            results: list[TriageRecallResult] = []
            for entry in candidates:
                reasons: list[str] = []

                # Tier 1: signature
                sig_score = 0.0
                if query_signature:
                    sig_score, reason = score_signature_match(
                        query_signature, entry.crash_signature
                    )
                    if reason:
                        reasons.append(reason)

                # Tier 2: stack hash
                stack_score = 0.0
                if query_stack_hash:
                    stack_score, reason = score_stack_hash_match(query_stack_hash, entry.stack_hash)
                    if reason:
                        reasons.append(reason)

                # Tier 3: TF-IDF
                tfidf_score = 0.0
                if query_tokens and entry.tokens:
                    tfidf_score = score_tfidf_similarity(
                        query_tokens, entry.tokens, df_lookup, total_docs
                    )
                    if tfidf_score > 0.1:
                        reasons.append(f"keyword similarity ({tfidf_score:.0%})")

                overall = compute_overall_score(
                    sig_score, stack_score, tfidf_score, entry.confidence
                )

                if overall > 0.01:
                    results.append(
                        TriageRecallResult(
                            entry=entry,
                            similarity_score=round(overall, 4),
                            match_reasons=reasons,
                        )
                    )

            # Sort by score descending and limit
            results.sort(key=lambda r: r.similarity_score, reverse=True)
            top = results[:limit]

            # Update hit counts and last_recalled_at for returned results
            self._mark_recalled([r.entry.id for r in top])

            return top

    def _get_recall_candidates(
        self,
        query_signature: str | None,
        query_stack_hash: str | None,
        query_tokens: list[str] | None,
    ) -> list[TriageMemoryEntry]:
        """Fetch candidate entries for recall scoring.

        Uses indexed lookups to narrow the candidate set before scoring.
        """
        assert self._conn is not None
        candidate_ids: set[str] = set()

        # Signature-based candidates
        if query_signature:
            parts = query_signature.split("|")
            if len(parts) >= 2:
                # Exact + partial (same exception+module)
                cursor = self._conn.execute(
                    "SELECT id FROM triage_memory WHERE crash_signature = ? "
                    "OR (exception_type = ? AND faulting_module = ?)",
                    (query_signature, parts[0], parts[1]),
                )
                candidate_ids.update(row["id"] for row in cursor)

        # Stack hash candidates
        if query_stack_hash:
            cursor = self._conn.execute(
                "SELECT id FROM triage_memory WHERE stack_hash = ?",
                (query_stack_hash,),
            )
            candidate_ids.update(row["id"] for row in cursor)

        # Keyword candidates: top tokens by frequency
        if query_tokens and not candidate_ids:
            # Use most distinctive tokens to find candidates
            token_counts = Counter(query_tokens)
            top_tokens = [t for t, _ in token_counts.most_common(10)]
            for token in top_tokens[:5]:
                pattern = f"%{token}%"
                cursor = self._conn.execute(
                    "SELECT id FROM triage_memory WHERE raw_analysis_snippet LIKE ? "
                    "OR tokens LIKE ? LIMIT 50",
                    (pattern, pattern),
                )
                candidate_ids.update(row["id"] for row in cursor)
                if len(candidate_ids) >= 50:
                    break

        if not candidate_ids:
            # Fallback: most recent entries with decent confidence
            cursor = self._conn.execute(
                "SELECT id FROM triage_memory WHERE confidence > 0.1 "
                "ORDER BY updated_at DESC LIMIT 20"
            )
            candidate_ids.update(row["id"] for row in cursor)

        if not candidate_ids:
            return []

        # Load full entries
        placeholders = ", ".join("?" for _ in candidate_ids)
        cursor = self._conn.execute(
            f"SELECT * FROM triage_memory WHERE id IN ({placeholders})",
            list(candidate_ids),
        )
        return [_row_to_entry(row) for row in cursor]

    def _mark_recalled(self, entry_ids: list[str]) -> None:
        """Update hit_count and last_recalled_at for recalled entries."""
        if not entry_ids:
            return
        assert self._conn is not None
        now = datetime.now(timezone.utc).isoformat()
        for eid in entry_ids:
            self._conn.execute(
                "UPDATE triage_memory SET hit_count = hit_count + 1, "
                "last_recalled_at = ? WHERE id = ?",
                (now, eid),
            )
        self._conn.commit()

    # ------------------------------------------------------------------
    # List / Forget
    # ------------------------------------------------------------------

    def list_patterns(
        self,
        offset: int = 0,
        limit: int = 20,
        tag_filter: str | None = None,
    ) -> list[TriageMemoryEntry]:
        """List stored patterns, optionally filtered by tag."""
        with self._lock:
            assert self._conn is not None
            if tag_filter:
                pattern = f'%"{tag_filter}"%'
                cursor = self._conn.execute(
                    "SELECT * FROM triage_memory WHERE tags LIKE ? "
                    "ORDER BY confidence DESC, updated_at DESC LIMIT ? OFFSET ?",
                    (pattern, limit, offset),
                )
            else:
                cursor = self._conn.execute(
                    "SELECT * FROM triage_memory "
                    "ORDER BY confidence DESC, updated_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            return [_row_to_entry(row) for row in cursor]

    def forget(self, pattern_id: str) -> bool:
        """Delete a memory entry by ID. Returns True if deleted."""
        with self._lock:
            assert self._conn is not None
            cursor = self._conn.execute("SELECT * FROM triage_memory WHERE id = ?", (pattern_id,))
            row = cursor.fetchone()
            if not row:
                return False
            entry = _row_to_entry(row)
            self._update_df(entry.tokens, increment=False)
            self._conn.execute("DELETE FROM triage_memory WHERE id = ?", (pattern_id,))
            self._conn.commit()
            logger.info("Deleted triage memory: %s", pattern_id)
            return True

    def get_by_dump_path(self, dump_path: str) -> TriageMemoryEntry | None:
        """Look up the most recent entry for a dump path."""
        with self._lock:
            assert self._conn is not None
            cursor = self._conn.execute(
                "SELECT * FROM triage_memory WHERE dump_path = ? ORDER BY updated_at DESC LIMIT 1",
                (dump_path,),
            )
            row = cursor.fetchone()
            return _row_to_entry(row) if row else None

    def update_entry(self, pattern_id: str, **kwargs: object) -> bool:
        """Update specific fields of an entry by ID."""
        with self._lock:
            assert self._conn is not None
            allowed = {
                "root_cause",
                "fix_description",
                "fix_pr_url",
                "tags",
                "debugger_commands_used",
                "confidence",
            }
            updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
            if not updates:
                return False

            # JSON-encode list fields
            for key in ("tags", "debugger_commands_used"):
                if key in updates and isinstance(updates[key], list):
                    updates[key] = json.dumps(updates[key])

            updates["updated_at"] = datetime.now(timezone.utc).isoformat()
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [pattern_id]
            cursor = self._conn.execute(
                f"UPDATE triage_memory SET {set_clause} WHERE id = ?", values
            )
            self._conn.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Confidence decay & pruning
    # ------------------------------------------------------------------

    def decay_confidence(self, half_life_days: float = 90.0) -> int:
        """Apply confidence decay to all entries based on time since last update.

        Returns the number of entries updated.
        """
        with self._lock:
            assert self._conn is not None
            now = datetime.now(timezone.utc)
            cursor = self._conn.execute("SELECT id, updated_at, confidence FROM triage_memory")
            updates: list[tuple[float, str]] = []

            for row in cursor:
                updated = datetime.fromisoformat(row["updated_at"])
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                days_elapsed = (now - updated).total_seconds() / 86400.0
                decayed = row["confidence"] * (0.5 ** (days_elapsed / half_life_days))
                decayed = round(decayed, 6)
                if abs(decayed - row["confidence"]) > 0.001:
                    updates.append((decayed, row["id"]))

            for conf, eid in updates:
                self._conn.execute(
                    "UPDATE triage_memory SET confidence = ? WHERE id = ?",
                    (conf, eid),
                )
            self._conn.commit()
            return len(updates)

    def prune(self, max_entries: int = 10000, min_confidence: float = 0.05) -> int:
        """Remove low-confidence entries and enforce max entry count.

        Returns the number of entries pruned.
        """
        with self._lock:
            assert self._conn is not None
            pruned = 0

            # Remove entries below min confidence
            cursor = self._conn.execute(
                "DELETE FROM triage_memory WHERE confidence < ?", (min_confidence,)
            )
            pruned += cursor.rowcount

            # Enforce max entries (remove oldest/lowest confidence)
            count = self._count_entries_unlocked()
            if count > max_entries:
                excess = count - max_entries
                cursor = self._conn.execute(
                    "DELETE FROM triage_memory WHERE id IN ("
                    "  SELECT id FROM triage_memory "
                    "  ORDER BY confidence ASC, updated_at ASC LIMIT ?"
                    ")",
                    (excess,),
                )
                pruned += cursor.rowcount

            if pruned:
                self._conn.commit()
                logger.info("Pruned %d memory entries", pruned)
            return pruned

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Return summary statistics about the memory store."""
        with self._lock:
            assert self._conn is not None
            count = self._count_entries_unlocked()
            if count == 0:
                return {"total_entries": 0}

            oldest = self._conn.execute("SELECT MIN(created_at) FROM triage_memory").fetchone()[0]
            newest = self._conn.execute("SELECT MAX(created_at) FROM triage_memory").fetchone()[0]
            avg_conf = self._conn.execute("SELECT AVG(confidence) FROM triage_memory").fetchone()[0]

            # Top tags
            cursor = self._conn.execute("SELECT tags FROM triage_memory")
            tag_counts: Counter[str] = Counter()
            for row in cursor:
                for tag in json.loads(row["tags"] or "[]"):
                    tag_counts[tag] += 1

            return {
                "total_entries": count,
                "oldest_entry": oldest,
                "newest_entry": newest,
                "avg_confidence": round(avg_conf, 3) if avg_conf else 0.0,
                "top_tags": dict(tag_counts.most_common(10)),
            }

    # ------------------------------------------------------------------
    # TF-IDF document frequency helpers
    # ------------------------------------------------------------------

    def _update_df(self, tokens: list[str], *, increment: bool) -> None:
        """Update document frequency table for TF-IDF."""
        if not tokens:
            return
        assert self._conn is not None
        unique = set(tokens)
        delta = 1 if increment else -1
        for token in unique:
            self._conn.execute(
                "INSERT INTO df_table (token, doc_count) VALUES (?, ?)"
                " ON CONFLICT(token) DO UPDATE SET doc_count = MAX(0, doc_count + ?)",
                (token, max(0, delta), delta),
            )

    def _load_df_lookup(self) -> dict[str, int]:
        """Load the document frequency table into a dict."""
        assert self._conn is not None
        cursor = self._conn.execute("SELECT token, doc_count FROM df_table")
        return {row["token"]: row["doc_count"] for row in cursor}

    def _count_entries(self) -> int:
        with self._lock:
            return self._count_entries_unlocked()

    def _count_entries_unlocked(self) -> int:
        assert self._conn is not None
        return self._conn.execute("SELECT COUNT(*) FROM triage_memory").fetchone()[0]
