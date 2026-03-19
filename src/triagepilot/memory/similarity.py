"""Lightweight TF-IDF similarity engine for crash triage memory.

Pure Python implementation — no numpy/scipy/sklearn required.
"""

from __future__ import annotations

import math
from collections import Counter


def compute_tf(tokens: list[str]) -> dict[str, float]:
    """Compute term frequency (normalized by document length)."""
    if not tokens:
        return {}
    counts = Counter(tokens)
    length = len(tokens)
    return {term: count / length for term, count in counts.items()}


def compute_idf(token: str, doc_count: int, total_docs: int) -> float:
    """Compute inverse document frequency with smoothing."""
    if total_docs == 0:
        return 0.0
    return math.log((1 + total_docs) / (1 + doc_count)) + 1.0


def cosine_similarity(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
    """Compute cosine similarity between two sparse TF-IDF vectors."""
    if not vec_a or not vec_b:
        return 0.0

    # Dot product (only on shared keys)
    shared_keys = set(vec_a) & set(vec_b)
    if not shared_keys:
        return 0.0

    dot = sum(vec_a[k] * vec_b[k] for k in shared_keys)
    norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
    norm_b = math.sqrt(sum(v * v for v in vec_b.values()))

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Multi-tier crash similarity scoring
# ---------------------------------------------------------------------------

# Weights for each tier
TIER1_WEIGHT = 0.5  # Crash signature match
TIER2_WEIGHT = 0.3  # Stack hash match
TIER3_WEIGHT = 0.2  # TF-IDF keyword similarity


def score_signature_match(query_sig: str, candidate_sig: str) -> tuple[float, str | None]:
    """Score crash signature similarity (Tier 1).

    Returns (score, match_reason) where score is 0.0-1.0.
    """
    if not query_sig or not candidate_sig:
        return 0.0, None

    if query_sig == candidate_sig:
        return 1.0, "exact signature match"

    # Partial match: split on | and compare components
    q_parts = query_sig.split("|")
    c_parts = candidate_sig.split("|")

    if len(q_parts) != 4 or len(c_parts) != 4:
        return 0.0, None

    # Same exception + module = partial match
    if q_parts[0] == c_parts[0] and q_parts[1] == c_parts[1]:
        # Same exception + module + function = strong partial
        if q_parts[2] == c_parts[2]:
            return 0.8, "same exception, module, and function (different offset)"
        return 0.5, "same exception type and module"

    # Same exception type only
    if q_parts[0] == c_parts[0] and q_parts[0] != "UNKNOWN":
        return 0.2, "same exception type"

    return 0.0, None


def score_stack_hash_match(
    query_hash: str | None, candidate_hash: str | None
) -> tuple[float, str | None]:
    """Score stack hash similarity (Tier 2).

    Returns (score, match_reason).
    """
    if not query_hash or not candidate_hash:
        return 0.0, None
    if query_hash == candidate_hash:
        return 1.0, "identical stack trace (top 5 frames)"
    return 0.0, None


def score_tfidf_similarity(
    query_tokens: list[str],
    candidate_tokens: list[str],
    df_lookup: dict[str, int],
    total_docs: int,
) -> float:
    """Score TF-IDF keyword similarity (Tier 3).

    Parameters
    ----------
    query_tokens: Tokens from the query analysis.
    candidate_tokens: Pre-computed tokens from the stored entry.
    df_lookup: Document frequency for each token.
    total_docs: Total number of documents in the store.

    Returns
    -------
    Cosine similarity score 0.0-1.0.
    """
    if not query_tokens or not candidate_tokens:
        return 0.0

    # Build TF-IDF vectors
    q_tf = compute_tf(query_tokens)
    c_tf = compute_tf(candidate_tokens)

    q_tfidf = {
        term: tf * compute_idf(term, df_lookup.get(term, 0), total_docs)
        for term, tf in q_tf.items()
    }
    c_tfidf = {
        term: tf * compute_idf(term, df_lookup.get(term, 0), total_docs)
        for term, tf in c_tf.items()
    }

    return cosine_similarity(q_tfidf, c_tfidf)


def compute_overall_score(
    sig_score: float,
    stack_score: float,
    tfidf_score: float,
    confidence: float,
) -> float:
    """Compute the final similarity score combining all tiers.

    The result is multiplied by the entry's confidence (which decays over time).
    """
    raw = sig_score * TIER1_WEIGHT + stack_score * TIER2_WEIGHT + tfidf_score * TIER3_WEIGHT
    return raw * confidence
