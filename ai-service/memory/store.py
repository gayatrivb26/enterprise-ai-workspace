"""
memory/store.py — long-term memory retrieval (Phase 6).

Recency alone is a poor proxy for relevance: the newest thing a user committed
to is very often not the thing their current question is about. This ranks
memories by a blend instead:

    score = relevance * W_REL + recency * W_REC + importance * W_IMP

**relevance** is genuine semantic similarity, using the same embedding model as
the document corpus, so "what did I promise about billing?" matches "Own the
payments migration plan" without sharing a word.

**recency** decays smoothly rather than sorting, so a highly relevant six-week-old
decision can still outrank an irrelevant one from yesterday.

**importance** comes from the entry type: a decision outlives an episodic note.

Embeddings are cached in-process by content hash. A workspace has tens or low
hundreds of memories and their text never changes once written, so the cache
hits almost every time and the model runs only for genuinely new entries.
"""
from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.db import get_conn

log = logging.getLogger(__name__)

# Blend weights. Relevance dominates; the others break ties and stop a stale
# but perfectly-matching entry from crowding out a fresher one.
W_RELEVANCE = 0.60
W_RECENCY = 0.25
W_IMPORTANCE = 0.15

# Memories much older than this contribute little recency, but can still win
# on relevance alone.
RECENCY_HALF_LIFE_DAYS = 30.0

IMPORTANCE = {
    "decision": 1.0,
    "fact": 0.85,
    "preference": 0.8,
    "episodic": 0.6,
}

# Below this combined score a memory is not worth the prompt space.
MIN_SCORE = 0.25

# Bounds the work per query regardless of how much has accumulated.
CANDIDATE_LIMIT = 200

_embedding_cache: dict[str, list[float]] = {}


@dataclass
class Memory:
    content: str
    entry_type: str
    created_at: datetime | None
    score: float = 0.0


# ── Scoring ─────────────────────────────────────────────────────────────────

def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    # Map [-1, 1] onto [0, 1] so it blends with the other two terms.
    return max(0.0, min(1.0, (dot / (na * nb) + 1.0) / 2.0))


def _recency(created_at: datetime | None) -> float:
    """Exponential decay — smooth, so nothing falls off a cliff."""
    if created_at is None:
        return 0.5
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (datetime.now(timezone.utc) - created_at) / timedelta(days=1))
    return math.exp(-age_days / RECENCY_HALF_LIFE_DAYS)


def importance_of(entry_type: str) -> float:
    return IMPORTANCE.get(entry_type, 0.7)


def _key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _embed(texts: list[str]) -> list[list[float]]:
    """Embed with an in-process cache keyed on content hash."""
    from rag.embedding import embed_texts

    missing = [t for t in texts if _key(t) not in _embedding_cache]
    if missing:
        try:
            for text, vector in zip(missing, embed_texts(missing)):
                _embedding_cache[_key(text)] = list(vector)
        except Exception as e:
            # Without embeddings this degrades to recency + importance, which
            # is exactly the previous behaviour — never an error.
            log.warning("Could not embed memories (%s); ranking without relevance.", e)
            return []
    return [_embedding_cache[_key(t)] for t in texts]


def score_memories(memories: list[Memory], query_vector, memory_vectors) -> list[Memory]:
    """Apply the blend. Split out so it can be tested without a database."""
    for index, memory in enumerate(memories):
        relevance = (
            _cosine(query_vector, memory_vectors[index])
            if query_vector is not None and index < len(memory_vectors)
            else 0.5  # neutral when embeddings are unavailable
        )
        memory.score = (
            relevance * W_RELEVANCE
            + _recency(memory.created_at) * W_RECENCY
            + importance_of(memory.entry_type) * W_IMPORTANCE
        )
    return sorted(memories, key=lambda m: m.score, reverse=True)


# ── Retrieval ───────────────────────────────────────────────────────────────

def _load_candidates(user_id: str, limit: int = CANDIDATE_LIMIT) -> list[Memory]:
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT content, type, created_at
                  FROM memory_entries
                 WHERE user_id = %s
                 ORDER BY created_at DESC
                 LIMIT %s
                """,
                (user_id, limit),
            )
            return [Memory(content=r[0], entry_type=r[1], created_at=r[2])
                    for r in cur.fetchall()]
    except Exception as e:
        log.warning("Could not load memories for %s: %s", user_id, e)
        return []


def get_relevant_memory(user_id: str, query: str, limit: int = 5) -> list[str]:
    """
    The memories worth putting in front of the model for this question.
    Returns plain strings so callers can drop them straight into a prompt.
    """
    candidates = _load_candidates(user_id)
    if not candidates:
        return []

    query = (query or "").strip()
    vectors = _embed([m.content for m in candidates] + [query]) if query else []

    if vectors:
        *memory_vectors, query_vector = vectors
    else:
        memory_vectors, query_vector = [], None

    ranked = score_memories(candidates, query_vector, memory_vectors)
    return [m.content for m in ranked[:limit] if m.score >= MIN_SCORE]


def prune_stale_memory(days_unused: int = 60) -> int:
    """
    Report how many entries have gone unused for `days_unused` days.

    Deliberately not a delete: an old commitment is still evidence of what was
    agreed, and losing it silently would be worse than ranking it low.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_unused)
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM memory_entries WHERE last_used_at < %s", (cutoff,))
            return int((cur.fetchone() or [0])[0])
    except Exception as e:
        log.warning("Could not prune memory: %s", e)
        return 0


def touch(user_id: str, contents: list[str]) -> None:
    """Mark memories as used, which feeds the recency term next time."""
    if not contents:
        return
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE memory_entries SET last_used_at = now() "
                "WHERE user_id = %s AND content = ANY(%s)",
                (user_id, contents),
            )
    except Exception as e:
        log.debug("Could not update memory usage: %s", e)
