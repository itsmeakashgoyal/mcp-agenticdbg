"""Tests for the persistent memory system."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from triagepilot.memory.models import TriageMemoryEntry
from triagepilot.memory.signature import (
    compute_stack_hash,
    extract_auto_tags,
    extract_crash_signature,
    tokenize_for_search,
)
from triagepilot.memory.similarity import (
    compute_overall_score,
    compute_tf,
    cosine_similarity,
    score_signature_match,
    score_stack_hash_match,
)
from triagepilot.memory.store import MemoryStore

# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary database path."""
    return str(tmp_path / "test_memory.db")


@pytest.fixture
def store(tmp_db):
    """Create a MemoryStore with a temporary database."""
    s = MemoryStore(db_path=tmp_db)
    yield s
    s.close()


@pytest.fixture
def sample_entry():
    """Create a sample TriageMemoryEntry."""
    return TriageMemoryEntry(
        dump_path="/tmp/core.12345",
        platform="linux",
        debugger_type="gdb",
        crash_signature="SIGSEGV|myapp|process_data|0x0-0xFF",
        exception_type="SIGSEGV",
        faulting_module="myapp",
        faulting_function="process_data",
        faulting_file="src/data.cpp",
        faulting_line=42,
        stack_hash="abc123def456",
        root_cause="Null pointer dereference when input buffer is empty",
        fix_description="Added null check before accessing buffer",
        tags=["sigsegv", "null-deref", "lang:c++"],
        debugger_commands_used=["bt full", "info registers", "x/10x $rsp"],
        raw_analysis_snippet="Program received signal SIGSEGV, Segmentation fault.\n"
        "0x0000555555555169 in process_data (buf=0x0) at src/data.cpp:42\n"
        "42\t    return buf->size;\n",
        tokens=[
            "program",
            "received",
            "signal",
            "sigsegv",
            "segmentation",
            "fault",
            "process_data",
            "buf",
            "src",
            "data",
            "cpp",
            "size",
        ],
    )


# ===========================================================================
# TestSignatureExtraction
# ===========================================================================


class TestSignatureExtraction:
    """Tests for crash signature extraction from debugger output."""

    GDB_ANALYSIS = """\
Program received signal SIGSEGV, Segmentation fault.
0x0000555555555169 in process_data (buf=0x0) at src/data.cpp:42
42	    return buf->size;
#0  0x0000555555555169 in process_data (buf=0x0) at src/data.cpp:42
#1  0x00005555555551a0 in main (argc=1, argv=0x7fffffffe0a8) at src/main.cpp:15
"""

    LLDB_ANALYSIS = """\
* thread #1, queue = 'com.apple.main-thread', stop reason = EXC_BAD_ACCESS (code=1, address=0x0)
    frame #0: 0x0000000100003f50 myapp`process_data(buf=0x0000000000000000) at data.cpp:42:12
    frame #1: 0x0000000100003f90 myapp`main(argc=1, argv=0x00007ffeefbff5b8) at main.cpp:15:10
"""

    CDB_ANALYSIS = """\
FAULTING_SOURCE_FILE: C:\\src\\data.cpp
FAULTING_SOURCE_LINE_NUMBER: 42
MODULE_NAME: myapp
SYMBOL_NAME: myapp!ProcessData+0x9e
ExceptionCode: 0xc0000005 (EXCEPTION_ACCESS_VIOLATION)
BUGCHECK_STR: ACCESS_VIOLATION
STACK_TEXT:
myapp!ProcessData+0x9e
myapp!main+0x42
"""

    def test_extract_gdb_signature(self):
        sig = extract_crash_signature(self.GDB_ANALYSIS, "gdb")
        assert sig.exception_type == "SIGSEGV"
        assert sig.faulting_function == "process_data"
        assert sig.faulting_file == "src/data.cpp"
        assert sig.faulting_line == 42

    def test_extract_lldb_signature(self):
        sig = extract_crash_signature(self.LLDB_ANALYSIS, "lldb")
        assert sig.exception_type == "EXC_BAD_ACCESS"

    def test_extract_cdb_signature(self):
        sig = extract_crash_signature(self.CDB_ANALYSIS, "cdb")
        assert sig.exception_type == "ACCESS_VIOLATION"
        assert sig.faulting_module == "myapp"
        assert sig.faulting_function == "ProcessData"
        assert sig.faulting_file == "C:\\src\\data.cpp"
        assert sig.faulting_line == 42

    def test_normalized_signature(self):
        sig = extract_crash_signature(self.GDB_ANALYSIS, "gdb")
        normalized = sig.normalized()
        assert "SIGSEGV" in normalized
        assert "process_data" in normalized

    def test_auto_detect_gdb(self):
        sig = extract_crash_signature(self.GDB_ANALYSIS, "auto")
        assert sig.exception_type == "SIGSEGV"

    def test_compute_stack_hash_gdb(self):
        h = compute_stack_hash(self.GDB_ANALYSIS)
        assert h is not None
        assert len(h) == 64  # SHA256 hex

    def test_compute_stack_hash_deterministic(self):
        h1 = compute_stack_hash(self.GDB_ANALYSIS)
        h2 = compute_stack_hash(self.GDB_ANALYSIS)
        assert h1 == h2

    def test_compute_stack_hash_empty(self):
        assert compute_stack_hash("no stack frames here") is None


class TestAutoTags:
    def test_basic_tags(self):
        tags = extract_auto_tags(
            "Program received signal SIGSEGV, null pointer dereference",
            debugger_type="gdb",
            faulting_file="src/main.cpp",
            exception_type="SIGSEGV",
            faulting_module="myapp",
        )
        assert "sigsegv" in tags
        assert "module:myapp" in tags
        assert "debugger:gdb" in tags
        assert "lang:c++" in tags
        assert "null-deref" in tags

    def test_use_after_free_tag(self):
        tags = extract_auto_tags("heap-use-after-free detected")
        assert "use-after-free" in tags

    def test_no_duplicate_tags(self):
        tags = extract_auto_tags(
            "SIGSEGV null pointer",
            exception_type="SIGSEGV",
        )
        assert tags.count("sigsegv") == 1


class TestTokenization:
    def test_basic_tokenization(self):
        tokens = tokenize_for_search("process_data buf size return")
        assert "process_data" in tokens
        assert "buf" in tokens
        assert "size" in tokens

    def test_hex_removal(self):
        tokens = tokenize_for_search("0x0000555555555169 in process_data 0xdeadbeef")
        assert "process_data" in tokens
        # hex addresses should be removed
        for t in tokens:
            assert not t.startswith("0x")

    def test_stopword_removal(self):
        tokens = tokenize_for_search("the function is not working for this case")
        assert "the" not in tokens
        assert "not" not in tokens
        assert "function" in tokens

    def test_file_path_tokens(self):
        tokens = tokenize_for_search("crash", faulting_file="src/data/processor.cpp")
        assert "data" in tokens
        assert "processor" in tokens

    def test_tags_included(self):
        tokens = tokenize_for_search("crash", tags=["sigsegv", "null-deref"])
        assert "sigsegv" in tokens
        assert "null-deref" in tokens


# ===========================================================================
# TestSimilarity
# ===========================================================================


class TestSimilarity:
    def test_compute_tf(self):
        tf = compute_tf(["a", "b", "a", "c"])
        assert tf["a"] == pytest.approx(0.5)
        assert tf["b"] == pytest.approx(0.25)

    def test_compute_tf_empty(self):
        assert compute_tf([]) == {}

    def test_cosine_similarity_identical(self):
        vec = {"a": 1.0, "b": 2.0}
        assert cosine_similarity(vec, vec) == pytest.approx(1.0)

    def test_cosine_similarity_orthogonal(self):
        assert cosine_similarity({"a": 1.0}, {"b": 1.0}) == pytest.approx(0.0)

    def test_cosine_similarity_empty(self):
        assert cosine_similarity({}, {"a": 1.0}) == 0.0

    def test_signature_exact_match(self):
        score, reason = score_signature_match(
            "SIGSEGV|myapp|func|0x0-0xFF",
            "SIGSEGV|myapp|func|0x0-0xFF",
        )
        assert score == 1.0
        assert "exact" in reason

    def test_signature_partial_match(self):
        score, reason = score_signature_match(
            "SIGSEGV|myapp|func_a|0x0-0xFF",
            "SIGSEGV|myapp|func_b|0x100-0xFFF",
        )
        assert score == 0.5
        assert "module" in reason

    def test_signature_same_func_different_offset(self):
        score, reason = score_signature_match(
            "SIGSEGV|myapp|func|0x0-0xFF",
            "SIGSEGV|myapp|func|0x100-0xFFF",
        )
        assert score == 0.8

    def test_signature_no_match(self):
        score, reason = score_signature_match(
            "SIGSEGV|myapp|func|0x0-0xFF",
            "SIGABRT|other|crash|0x0-0xFF",
        )
        assert score == 0.0

    def test_stack_hash_match(self):
        score, reason = score_stack_hash_match("abc123", "abc123")
        assert score == 1.0

    def test_stack_hash_no_match(self):
        score, reason = score_stack_hash_match("abc123", "def456")
        assert score == 0.0

    def test_overall_score_with_confidence(self):
        score = compute_overall_score(1.0, 1.0, 1.0, confidence=0.5)
        assert score == pytest.approx(0.5)

    def test_overall_score_zero_confidence(self):
        score = compute_overall_score(1.0, 1.0, 1.0, confidence=0.0)
        assert score == 0.0


# ===========================================================================
# TestMemoryStore
# ===========================================================================


class TestMemoryStore:
    def test_create_store(self, store):
        assert os.path.exists(store.db_path)

    def test_save_and_retrieve(self, store, sample_entry):
        entry_id = store.save(sample_entry)
        assert entry_id == sample_entry.id

        retrieved = store.get_by_dump_path("/tmp/core.12345")
        assert retrieved is not None
        assert retrieved.crash_signature == sample_entry.crash_signature
        assert retrieved.root_cause == sample_entry.root_cause

    def test_upsert_same_signature(self, store, sample_entry):
        store.save(sample_entry)

        # Save another with same signature + stack hash
        duplicate = TriageMemoryEntry(
            dump_path="/tmp/core.99999",
            crash_signature=sample_entry.crash_signature,
            stack_hash=sample_entry.stack_hash,
            fix_description="Better fix",
        )
        store.save(duplicate)

        # Should update, not duplicate
        patterns = store.list_patterns()
        assert len(patterns) == 1
        # Original root_cause preserved, new fix not overwritten since original had one
        assert patterns[0].root_cause == sample_entry.root_cause

    def test_upsert_merges_tags(self, store, sample_entry):
        store.save(sample_entry)

        duplicate = TriageMemoryEntry(
            dump_path="/tmp/core.99999",
            crash_signature=sample_entry.crash_signature,
            stack_hash=sample_entry.stack_hash,
            tags=["new-tag"],
        )
        store.save(duplicate)

        patterns = store.list_patterns()
        assert "new-tag" in patterns[0].tags

    def test_recall_by_signature(self, store, sample_entry):
        store.save(sample_entry)

        results = store.recall(query_signature=sample_entry.crash_signature)
        assert len(results) >= 1
        assert results[0].entry.crash_signature == sample_entry.crash_signature
        assert results[0].similarity_score > 0

    def test_recall_by_stack_hash(self, store, sample_entry):
        store.save(sample_entry)

        results = store.recall(query_stack_hash=sample_entry.stack_hash)
        assert len(results) >= 1

    def test_recall_by_tokens(self, store, sample_entry):
        store.save(sample_entry)

        results = store.recall(query_tokens=["sigsegv", "process_data", "segmentation"])
        assert len(results) >= 1

    def test_recall_empty_store(self, store):
        results = store.recall(query_signature="SIGSEGV|foo|bar|0")
        assert results == []

    def test_list_patterns(self, store, sample_entry):
        store.save(sample_entry)
        patterns = store.list_patterns()
        assert len(patterns) == 1
        assert patterns[0].id == sample_entry.id

    def test_list_patterns_with_tag_filter(self, store, sample_entry):
        store.save(sample_entry)

        # Match
        patterns = store.list_patterns(tag_filter="sigsegv")
        assert len(patterns) == 1

        # No match
        patterns = store.list_patterns(tag_filter="nonexistent")
        assert len(patterns) == 0

    def test_forget(self, store, sample_entry):
        store.save(sample_entry)
        assert store.forget(sample_entry.id) is True
        assert store.get_by_dump_path("/tmp/core.12345") is None

    def test_forget_nonexistent(self, store):
        assert store.forget("nonexistent-id") is False

    def test_update_entry(self, store, sample_entry):
        store.save(sample_entry)
        store.update_entry(sample_entry.id, root_cause="Updated root cause")
        entry = store.get_by_dump_path("/tmp/core.12345")
        assert entry.root_cause == "Updated root cause"

    def test_stats(self, store, sample_entry):
        store.save(sample_entry)
        stats = store.stats()
        assert stats["total_entries"] == 1
        assert "sigsegv" in stats["top_tags"]

    def test_stats_empty(self, store):
        stats = store.stats()
        assert stats["total_entries"] == 0

    def test_decay_confidence(self, store):
        old_entry = TriageMemoryEntry(
            dump_path="/tmp/old.core",
            crash_signature="SIGSEGV|old|func|0",
            confidence=1.0,
            updated_at=datetime.now(timezone.utc) - timedelta(days=90),
        )
        store.save(old_entry)

        updated = store.decay_confidence(half_life_days=90.0)
        assert updated == 1

        entry = store.get_by_dump_path("/tmp/old.core")
        assert entry.confidence < 0.6  # Should be ~0.5 after one half-life

    def test_prune(self, store):
        # Create entries with varying confidence
        for i in range(5):
            entry = TriageMemoryEntry(
                dump_path=f"/tmp/core.{i}",
                crash_signature=f"SIG|mod|func{i}|0",
                confidence=0.01 * (i + 1),
            )
            store.save(entry)

        pruned = store.prune(max_entries=10, min_confidence=0.03)
        assert pruned == 2  # entries with confidence 0.01 and 0.02

    def test_prune_max_entries(self, store):
        for i in range(10):
            entry = TriageMemoryEntry(
                dump_path=f"/tmp/core.{i}",
                crash_signature=f"SIG|mod|func{i}|0",
                confidence=0.5,
            )
            store.save(entry)

        pruned = store.prune(max_entries=5, min_confidence=0.01)
        assert pruned == 5
        stats = store.stats()
        assert stats["total_entries"] == 5

    def test_recall_updates_hit_count(self, store, sample_entry):
        store.save(sample_entry)

        store.recall(query_signature=sample_entry.crash_signature)
        entry = store.get_by_dump_path("/tmp/core.12345")
        assert entry.hit_count >= 1
        assert entry.last_recalled_at is not None


# ===========================================================================
# TestMemoryTools
# ===========================================================================


class TestMemoryTools:
    """Tests for memory MCP tool handlers."""

    @pytest.mark.asyncio
    async def test_handle_recall_similar(self, store, sample_entry):
        from triagepilot.memory.models import RecallSimilarParams
        from triagepilot.memory.tools import handle_recall_similar

        store.save(sample_entry)

        args = RecallSimilarParams(analysis_text="Program received signal SIGSEGV in process_data")
        results = await handle_recall_similar(args, store)
        assert len(results) == 1
        assert "Similar Past Crashes" in results[0].text or "No similar" in results[0].text

    @pytest.mark.asyncio
    async def test_handle_save_triage_new(self, store):
        from triagepilot.memory.models import SaveTriageParams
        from triagepilot.memory.tools import handle_save_triage

        args = SaveTriageParams(
            dump_path="/tmp/new.core",
            root_cause="Buffer overflow",
            tags=["overflow"],
        )
        results = await handle_save_triage(args, store)
        assert "Saved" in results[0].text

    @pytest.mark.asyncio
    async def test_handle_save_triage_update(self, store, sample_entry):
        from triagepilot.memory.models import SaveTriageParams
        from triagepilot.memory.tools import handle_save_triage

        store.save(sample_entry)

        args = SaveTriageParams(
            dump_path="/tmp/core.12345",
            fix_pr_url="https://github.com/org/repo/pull/42",
        )
        results = await handle_save_triage(args, store)
        assert "Updated" in results[0].text

        entry = store.get_by_dump_path("/tmp/core.12345")
        assert entry.fix_pr_url == "https://github.com/org/repo/pull/42"

    @pytest.mark.asyncio
    async def test_handle_list_patterns(self, store, sample_entry):
        from triagepilot.memory.models import ListPatternsParams
        from triagepilot.memory.tools import handle_list_patterns

        store.save(sample_entry)

        args = ListPatternsParams()
        results = await handle_list_patterns(args, store)
        assert "Known Crash Patterns" in results[0].text

    @pytest.mark.asyncio
    async def test_handle_forget_pattern(self, store, sample_entry):
        from triagepilot.memory.models import ForgetPatternParams
        from triagepilot.memory.tools import handle_forget_pattern

        store.save(sample_entry)

        args = ForgetPatternParams(pattern_id=sample_entry.id)
        results = await handle_forget_pattern(args, store)
        assert "Deleted" in results[0].text

    @pytest.mark.asyncio
    async def test_handle_forget_nonexistent(self, store):
        from triagepilot.memory.models import ForgetPatternParams
        from triagepilot.memory.tools import handle_forget_pattern

        args = ForgetPatternParams(pattern_id="nonexistent")
        results = await handle_forget_pattern(args, store)
        assert "No memory entry" in results[0].text


class TestAutoSaveRecall:
    """Tests for auto-save and auto-recall helpers."""

    def test_auto_save_analysis(self, store):
        from triagepilot.memory.tools import auto_save_analysis

        analysis = (
            "Program received signal SIGSEGV, Segmentation fault.\n"
            "0x0000555555555169 in process_data (buf=0x0) at src/data.cpp:42\n"
            "#0  0x0000555555555169 in process_data (buf=0x0) at src/data.cpp:42\n"
            "#1  0x00005555555551a0 in main () at src/main.cpp:15\n"
        )
        entry_id = auto_save_analysis(
            store, "/tmp/core.auto", analysis, debugger_type="gdb", platform="linux"
        )
        assert entry_id is not None

        entry = store.get_by_dump_path("/tmp/core.auto")
        assert entry is not None
        assert "SIGSEGV" in entry.crash_signature
        assert entry.platform == "linux"
        assert len(entry.tags) > 0
        assert len(entry.tokens) > 0

    def test_auto_recall_similar(self, store, sample_entry):
        from triagepilot.memory.tools import auto_recall_similar

        store.save(sample_entry)

        analysis = "Program received signal SIGSEGV in process_data at src/data.cpp:42"
        result = auto_recall_similar(store, analysis)
        # May or may not find matches depending on token overlap
        assert isinstance(result, str)

    def test_auto_save_bad_input(self, store):
        from triagepilot.memory.tools import auto_save_analysis

        # Should not crash on garbage input
        entry_id = auto_save_analysis(store, "/tmp/garbage", "not a real dump")
        # May still save with UNKNOWN signature
        assert entry_id is not None or entry_id is None  # just shouldn't raise
