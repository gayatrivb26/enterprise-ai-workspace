"""
Memory ranking.

The point of Phase 6 is that recall is driven by relevance, not by whatever was
written most recently. These tests pin that behaviour without needing a
database or the embedding model: the scoring blend is a pure function.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memory.store import Memory, importance_of, score_memories

NOW = datetime.now(timezone.utc)


def make(content: str, entry_type: str = "fact", days_old: float = 1.0) -> Memory:
    return Memory(content=content, entry_type=entry_type,
                  created_at=NOW - timedelta(days=days_old))


def test_relevance_beats_recency():
    """
    The whole reason this module exists: an old but on-topic memory must
    outrank a brand-new irrelevant one.
    """
    relevant_old = make("Own the payments migration plan", days_old=45)
    irrelevant_new = make("Ordered new laptop stickers", days_old=0.1)

    query = [1.0, 0.0]
    vectors = [[1.0, 0.0], [-1.0, 0.0]]  # first aligns with the query, second opposes

    ranked = score_memories([relevant_old, irrelevant_new], query, vectors)
    assert ranked[0] is relevant_old


def test_recency_breaks_ties_between_equally_relevant_memories():
    older = make("Same relevance", days_old=60)
    newer = make("Same relevance too", days_old=0.5)

    query = [1.0, 0.0]
    vectors = [[1.0, 0.0], [1.0, 0.0]]

    ranked = score_memories([older, newer], query, vectors)
    assert ranked[0] is newer


def test_importance_breaks_ties_between_equal_relevance_and_age():
    episodic = make("An aside", entry_type="episodic", days_old=5)
    decision = make("A decision", entry_type="decision", days_old=5)

    query = [1.0, 0.0]
    vectors = [[1.0, 0.0], [1.0, 0.0]]

    ranked = score_memories([episodic, decision], query, vectors)
    assert ranked[0] is decision


def test_decisions_outrank_episodic_notes_by_type():
    assert importance_of("decision") > importance_of("episodic")
    assert importance_of("unknown-type") > 0


def test_ranking_still_works_without_embeddings():
    """
    If the embedding model is unavailable the blend must degrade to
    recency + importance rather than failing.
    """
    old = make("Old", days_old=90)
    new = make("New", days_old=0.2)

    ranked = score_memories([old, new], None, [])
    assert ranked[0] is new
    assert all(m.score > 0 for m in ranked)


def test_scores_stay_within_a_sane_range():
    memories = [make("A", days_old=0), make("B", days_old=365)]
    ranked = score_memories(memories, [1.0, 0.0], [[1.0, 0.0], [0.0, 1.0]])
    assert all(0.0 <= m.score <= 1.0 for m in ranked)
