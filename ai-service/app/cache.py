"""
app/cache.py — Redis-backed LLM answer cache and live job-status channel.

Why a cache at all: in a document Q&A product the same handful of questions get
asked repeatedly ("what's the leave policy?"), and every repeat costs a full
retrieval + generation round trip. Caching the *answer* removes both the cost
and the latency.

Correctness is the hard part. An answer is only reusable when everything that
produced it is unchanged, so the key is derived from all of:

    user scope · document/collection scope · normalized question ·
    model · prompt version · the exact chunk ids retrieved

Including the retrieved chunk ids is what makes this safe rather than merely
fast: if re-ingestion changes what retrieval returns, the key changes too, so a
stale answer cannot be served even if invalidation were somehow missed.

On top of that, entries are *tagged* with the document ids they drew from, so
deleting or re-uploading a document eagerly drops every answer derived from it.

Redis being unavailable is never fatal — every function degrades to a miss.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Iterable

import redis

from app.config import settings

log = logging.getLogger(__name__)

_client: redis.Redis | None = None
_unavailable = False


def get_client() -> redis.Redis | None:
    """Lazily connect. Returns None (rather than raising) if Redis is down."""
    global _client, _unavailable
    if _unavailable:
        return None
    if _client is None:
        try:
            _client = redis.Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            _client.ping()
        except Exception as e:  # pragma: no cover - depends on deployment
            log.warning("Redis unavailable, LLM cache disabled: %s", e)
            _client = None
            _unavailable = True
    return _client


def reset_client() -> None:
    """Allow a later reconnect after a transient outage."""
    global _client, _unavailable
    _client = None
    _unavailable = False


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def normalize_question(question: str) -> str:
    """
    Fold away differences that cannot change the answer: surrounding
    whitespace, internal whitespace runs, case, and trailing punctuation.
    "What is our leave policy?" and "what is our leave policy" share a key.
    """
    q = _WS_RE.sub(" ", question).strip().lower()
    return q.rstrip("?!. ")


def answer_key(
    *,
    user_id: str,
    question: str,
    document_ids: Iterable[str],
    chunk_ids: Iterable[str],
    model: str,
    prompt_version: str,
    memory_fingerprint: str = "",
) -> str:
    payload = json.dumps(
        {
            # Answers are per-user because retrieval scope and injected memory
            # are per-user; sharing across users would leak context.
            "u": user_id,
            "q": normalize_question(question),
            "d": sorted(set(document_ids)),
            "c": sorted(set(chunk_ids)),
            "m": model,
            "p": prompt_version,
            "mem": memory_fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{settings.cache_prefix}:ans:{digest}"


def fingerprint(values: Iterable[str]) -> str:
    """Short stable digest of an arbitrary list of strings."""
    joined = "\x1f".join(values)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _tag_key(document_id: str) -> str:
    return f"{settings.cache_prefix}:tag:doc:{document_id}"


# ---------------------------------------------------------------------------
# Answer cache
# ---------------------------------------------------------------------------

def get_answer(key: str) -> dict[str, Any] | None:
    if not settings.cache_enabled:
        return None
    client = get_client()
    if client is None:
        return None
    try:
        raw = client.get(key)
    except Exception as e:
        log.warning("Cache read failed: %s", e)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def set_answer(key: str, value: dict[str, Any], document_ids: Iterable[str]) -> None:
    """Store an answer and tag it with every document it was derived from."""
    if not settings.cache_enabled:
        return
    client = get_client()
    if client is None:
        return
    try:
        pipe = client.pipeline()
        pipe.setex(key, settings.cache_ttl_seconds, json.dumps(value))
        for doc_id in set(document_ids):
            tag = _tag_key(doc_id)
            pipe.sadd(tag, key)
            # Tag sets outlive their entries slightly; expiring them prevents
            # unbounded growth from documents that are never touched again.
            pipe.expire(tag, settings.cache_ttl_seconds * 2)
        pipe.execute()
    except Exception as e:
        log.warning("Cache write failed: %s", e)


def invalidate_documents(document_ids: Iterable[str]) -> int:
    """
    Drop every cached answer derived from any of these documents. Called
    whenever a document finishes ingesting or is deleted.
    """
    client = get_client()
    if client is None:
        return 0
    removed = 0
    try:
        for doc_id in set(document_ids):
            tag = _tag_key(doc_id)
            keys = client.smembers(tag)
            if keys:
                removed += client.delete(*keys)
            client.delete(tag)
    except Exception as e:
        log.warning("Cache invalidation failed: %s", e)
    return removed


def invalidate_user(user_id: str) -> int:
    """Nuclear option, used when a user's corpus changes wholesale."""
    client = get_client()
    if client is None:
        return 0
    removed = 0
    try:
        pattern = f"{settings.cache_prefix}:ans:*"
        for key in client.scan_iter(match=pattern, count=500):
            raw = client.get(key)
            if raw and f'"u":"{user_id}"' in raw:
                removed += client.delete(key)
    except Exception as e:
        log.warning("User cache invalidation failed: %s", e)
    return removed


def stats() -> dict[str, Any]:
    client = get_client()
    if client is None:
        return {"available": False, "entries": 0}
    try:
        entries = sum(1 for _ in client.scan_iter(
            match=f"{settings.cache_prefix}:ans:*", count=500))
        hits = int(client.get(f"{settings.cache_prefix}:stat:hit") or 0)
        misses = int(client.get(f"{settings.cache_prefix}:stat:miss") or 0)
        total = hits + misses
        return {
            "available": True,
            "entries": entries,
            "hits": hits,
            "misses": misses,
            "hit_rate": round(hits / total, 3) if total else 0.0,
        }
    except Exception:
        return {"available": False, "entries": 0}


def record_hit(hit: bool) -> None:
    client = get_client()
    if client is None:
        return
    try:
        client.incr(f"{settings.cache_prefix}:stat:{'hit' if hit else 'miss'}")
    except Exception:
        pass
