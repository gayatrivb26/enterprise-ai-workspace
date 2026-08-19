"""
app/db.py — thin psycopg wrapper.

The AI service owns documents, document_chunks, memory_entries, collections
and the llm_usage ledger; ASP.NET Core remains the system of record for
users, chats and messages.
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row

from app.config import settings

log = logging.getLogger(__name__)


@contextmanager
def get_conn(row_factory=None):
    conn = psycopg.connect(settings.postgres_dsn, row_factory=row_factory or psycopg.rows.tuple_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

DOCUMENT_COLUMNS = """
    id, user_id, filename, type, status, progress, error,
    size_bytes, page_count, chunk_count, token_count,
    collection_id, uploaded_at, updated_at
"""


def create_document(
    user_id: str,
    filename: str,
    file_type: str,
    size_bytes: int = 0,
    content_hash: str | None = None,
    collection_id: str | None = None,
) -> str:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents
                (user_id, filename, type, status, progress, size_bytes,
                 content_hash, collection_id)
            VALUES (%s, %s, %s, 'queued', 0, %s, %s, %s)
            RETURNING id
            """,
            (user_id, filename, file_type, size_bytes, content_hash, collection_id),
        )
        return str(cur.fetchone()[0])


def update_document_progress(
    document_id: str,
    status: str,
    progress: int,
    *,
    error: str | None = None,
    page_count: int | None = None,
    chunk_count: int | None = None,
    token_count: int | None = None,
) -> None:
    """
    Advances a document through the ingestion pipeline. COALESCE keeps any
    counter that this particular stage doesn't know about.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE documents
               SET status      = %s,
                   progress    = %s,
                   error       = %s,
                   page_count  = COALESCE(%s, page_count),
                   chunk_count = COALESCE(%s, chunk_count),
                   token_count = COALESCE(%s, token_count),
                   updated_at  = now()
             WHERE id = %s
            """,
            (status, progress, error, page_count, chunk_count, token_count, document_id),
        )


def get_document(document_id: str) -> dict[str, Any] | None:
    with get_conn(dict_row) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {DOCUMENT_COLUMNS} FROM documents WHERE id = %s", (document_id,))
        return cur.fetchone()


def list_documents(
    user_id: str,
    search: str | None = None,
    status: str | None = None,
    collection_id: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["user_id = %s"]
    params: list[Any] = [user_id]

    if search:
        clauses.append("filename ILIKE %s")
        params.append(f"%{search}%")
    if status and status != "all":
        if status == "processing":
            clauses.append("status IN ('queued','parsing','chunking','embedding','indexing')")
        else:
            clauses.append("status = %s")
            params.append(status)
    if collection_id:
        clauses.append("collection_id = %s")
        params.append(collection_id)

    with get_conn(dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {DOCUMENT_COLUMNS} FROM documents "
            f"WHERE {' AND '.join(clauses)} ORDER BY uploaded_at DESC",
            params,
        )
        return cur.fetchall()


def find_duplicate(user_id: str, content_hash: str) -> dict[str, Any] | None:
    """Same bytes already ingested for this user — used to skip re-embedding."""
    with get_conn(dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {DOCUMENT_COLUMNS} FROM documents "
            "WHERE user_id = %s AND content_hash = %s AND status = 'ready' LIMIT 1",
            (user_id, content_hash),
        )
        return cur.fetchone()


def get_document_vector_ids(document_id: str) -> list[str]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT vector_id FROM document_chunks WHERE document_id = %s AND vector_id IS NOT NULL",
            (document_id,),
        )
        return [row[0] for row in cur.fetchall()]


def delete_document(document_id: str) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        # document_chunks cascades via its FK.
        cur.execute("DELETE FROM documents WHERE id = %s", (document_id,))


def clear_document_chunks(document_id: str) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM document_chunks WHERE document_id = %s", (document_id,))


def save_chunks(document_id: str, rows: list[dict]) -> None:
    if not rows:
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO document_chunks (document_id, chunk_text, chunk_metadata, vector_id)
            VALUES (%s, %s, %s, %s)
            """,
            [
                (document_id, r["chunk_text"], json.dumps(r["chunk_metadata"]), r["vector_id"])
                for r in rows
            ],
        )


def get_chunks_by_vector_ids(vector_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Backs citation previews: vector_id -> {text, metadata, document_id}."""
    ids = list(vector_ids)
    if not ids:
        return {}
    with get_conn(dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.vector_id, c.chunk_text, c.chunk_metadata, c.document_id, d.filename
              FROM document_chunks c
              JOIN documents d ON d.id = c.document_id
             WHERE c.vector_id = ANY(%s)
            """,
            (ids,),
        )
        return {r["vector_id"]: r for r in cur.fetchall()}


# Legacy name kept so older callers (eval harness, agents) keep working.
def set_document_status(document_id: str, status: str) -> None:
    progress = 100 if status in ("ready", "embedded") else 0
    update_document_progress(document_id, status, progress)


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

def list_collections(user_id: str) -> list[dict[str, Any]]:
    with get_conn(dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id, c.name, c.color, c.created_at,
                   COUNT(d.id) AS document_count
              FROM collections c
              LEFT JOIN documents d ON d.collection_id = c.id
             WHERE c.user_id = %s
             GROUP BY c.id
             ORDER BY c.created_at
            """,
            (user_id,),
        )
        return cur.fetchall()


def create_collection(user_id: str, name: str, color: str = "indigo") -> dict[str, Any]:
    with get_conn(dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO collections (user_id, name, color) VALUES (%s, %s, %s) "
            "RETURNING id, name, color, created_at",
            (user_id, name, color),
        )
        return cur.fetchone()


def delete_collection(collection_id: str) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM collections WHERE id = %s", (collection_id,))


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

def get_recent_memory(user_id: str, limit: int = 5) -> list[str]:
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT content FROM memory_entries
                WHERE user_id = %s
                ORDER BY last_used_at DESC
                LIMIT %s
                """,
                (user_id, limit),
            )
            return [row[0] for row in cur.fetchall()]
    except Exception as e:
        # Memory is an enhancement, never a hard dependency of answering.
        log.warning("Could not load memory for %s: %s", user_id, e)
        return []


def write_memory(user_id: str, entry_type: str, content: str) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO memory_entries (user_id, type, content) VALUES (%s, %s, %s)",
            (user_id, entry_type, content),
        )


# ---------------------------------------------------------------------------
# LLM usage ledger
# ---------------------------------------------------------------------------

def record_usage(
    *,
    user_id: str | None,
    chat_id: str | None,
    operation: str,
    model: str,
    prompt_version: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    cache_hit: bool,
    latency_ms: int,
) -> None:
    """Best-effort: telemetry must never break a user-facing answer."""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO llm_usage
                    (user_id, chat_id, operation, model, prompt_version,
                     tokens_in, tokens_out, cost_usd, cache_hit, latency_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (user_id, chat_id, operation, model, prompt_version,
                 tokens_in, tokens_out, cost_usd, cache_hit, latency_ms),
            )
    except Exception as e:
        log.warning("Could not record LLM usage: %s", e)


def usage_summary(user_id: str, days: int = 30) -> dict[str, Any]:
    try:
        with get_conn(dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)                                  AS calls,
                       COALESCE(SUM(tokens_in), 0)               AS tokens_in,
                       COALESCE(SUM(tokens_out), 0)              AS tokens_out,
                       COALESCE(SUM(cost_usd), 0)                AS cost_usd,
                       COUNT(*) FILTER (WHERE cache_hit)         AS cache_hits,
                       COALESCE(AVG(latency_ms), 0)::INT         AS avg_latency_ms
                  FROM llm_usage
                 WHERE user_id = %s AND created_at > now() - (%s || ' days')::INTERVAL
                """,
                (user_id, days),
            )
            row = cur.fetchone() or {}
            calls = row.get("calls", 0) or 0
            hits = row.get("cache_hits", 0) or 0
            return {
                "calls": calls,
                "tokens_in": row.get("tokens_in", 0),
                "tokens_out": row.get("tokens_out", 0),
                "cost_usd": float(row.get("cost_usd", 0) or 0),
                "cache_hits": hits,
                "cache_hit_rate": round(hits / calls, 3) if calls else 0.0,
                "avg_latency_ms": row.get("avg_latency_ms", 0),
            }
    except Exception as e:
        log.warning("Could not summarise usage: %s", e)
        return {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
                "cache_hits": 0, "cache_hit_rate": 0.0, "avg_latency_ms": 0}
