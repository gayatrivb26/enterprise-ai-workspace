"""
app/pipeline.py — document ingestion as an observable, staged background job.

Upload used to be synchronous: the HTTP request sat open through parsing,
chunking and embedding, so a large PDF either blocked the client for a minute
or timed out, and the UI had nothing to show but a spinner.

Now the request only persists the file's metadata and returns immediately; the
work runs in the background and reports itself through the document row:

    queued → parsing → chunking → embedding → indexing → ready
                                                       ↘ failed

Each stage writes a status and a progress percentage, which is what the
frontend's pipeline visualisation reads. Progress is monotonic and the terminal
states are guaranteed: every exit path lands on `ready` or `failed`, so a
document can never be stuck mid-pipeline in the UI.
"""
from __future__ import annotations

import hashlib
import logging
import traceback

from app import cache, storage
from app.config import settings
from app.db import (
    clear_document_chunks,
    get_document,
    get_document_vector_ids,
    save_chunks,
    update_document_progress,
)
from rag import parsers
from rag.chunking import Chunk, chunk_blocks, count_tokens
from rag.embedding import add_chunks, delete_vectors

log = logging.getLogger(__name__)

# (status, progress-at-start) for each stage, in order.
STAGES: list[tuple[str, int]] = [
    ("queued", 0),
    ("parsing", 10),
    ("chunking", 30),
    ("embedding", 45),
    ("indexing", 90),
    ("ready", 100),
]

STAGE_ORDER = [s for s, _ in STAGES]


def content_hash(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()


# Extension -> logical type. Mirrors api/Services/FileValidator.cs, which
# additionally verifies the leading bytes before anything reaches these parsers.
_TYPES = {
    ".pdf": "pdf",
    ".md": "markdown", ".markdown": "markdown",
    ".txt": "text", ".text": "text", ".log": "text", ".csv": "text",
    ".docx": "word",
    ".xlsx": "excel",
    ".pptx": "powerpoint",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
}


def detect_type(filename: str) -> str | None:
    lowered = filename.lower()
    for suffix, kind in _TYPES.items():
        if lowered.endswith(suffix):
            return kind
    return None


def ingest(document_id: str, filename: str, file_type: str, file_bytes: bytes) -> None:
    """
    Run the full pipeline for one document. Safe to call from a FastAPI
    BackgroundTask or a worker process — it owns its own DB connections and
    never raises into the caller.
    """
    try:
        _run(document_id, filename, file_type, file_bytes)
    except Exception as e:
        log.error("Ingestion failed for %s: %s\n%s", document_id, e, traceback.format_exc())
        _fail(document_id, str(e))


def reingest(document_id: str) -> None:
    """
    Re-run the pipeline from the stored source file. This is what makes the
    UI's Retry action real rather than decorative.
    """
    row = get_document(document_id)
    if row is None:
        log.warning("Re-ingest requested for unknown document %s", document_id)
        return

    file_bytes = storage.load(document_id)
    if file_bytes is None:
        update_document_progress(
            document_id, "failed", 100,
            error="The original file is no longer available. Please upload it again.",
        )
        return

    update_document_progress(document_id, "queued", 0, error=None)
    ingest(document_id, row["filename"], row["type"], file_bytes)


def _fail(document_id: str, message: str) -> None:
    # Roll back partial work so a retry starts from a clean slate rather than
    # duplicating chunks into the vector store.
    try:
        vector_ids = get_document_vector_ids(document_id)
        if vector_ids:
            delete_vectors(vector_ids)
        clear_document_chunks(document_id)
    except Exception as cleanup_error:
        log.warning("Cleanup after failed ingest of %s failed: %s", document_id, cleanup_error)

    update_document_progress(
        document_id, "failed", 100, error=_readable(message), chunk_count=0
    )


def _readable(message: str) -> str:
    message = message.strip() or "Ingestion failed."
    return message if len(message) <= 300 else message[:297] + "…"


def _run(document_id: str, filename: str, file_type: str, file_bytes: bytes) -> None:
    # Re-ingesting an existing document: clear the old vectors first so the
    # corpus never contains two generations of the same file.
    existing = get_document_vector_ids(document_id)
    if existing:
        delete_vectors(existing)
        clear_document_chunks(document_id)

    # ── parsing ─────────────────────────────────────────────────────────
    update_document_progress(document_id, "parsing", 10)

    try:
        blocks = parsers.parse(file_type, file_bytes, filename)
    except parsers.UnsupportedFile:
        raise
    except Exception as e:
        # These parsers read attacker-supplied binaries; a malformed file must
        # surface as a readable failure, not an opaque worker crash.
        raise ValueError(f"This file could not be read: {e}") from e

    # Pages exist for PDFs and slide decks; sheets and documents have none.
    pages = {b.metadata.get("page") for b in blocks if b.metadata.get("page")}
    page_count = len(pages) or None

    update_document_progress(document_id, "parsing", 25, page_count=page_count)

    # ── chunking ────────────────────────────────────────────────────────
    update_document_progress(document_id, "chunking", 30, page_count=page_count)

    chunks: list[Chunk] = chunk_blocks(
        blocks,
        source_path=filename,
        max_tokens=settings.chunk_size_tokens,
        overlap_tokens=settings.chunk_overlap_tokens,
    )

    if not chunks:
        raise ValueError("Nothing could be extracted from this file.")

    token_count = sum(count_tokens(c.text) for c in chunks)
    update_document_progress(
        document_id, "chunking", 42,
        page_count=page_count, chunk_count=len(chunks), token_count=token_count,
    )

    # ── embedding ───────────────────────────────────────────────────────
    # Batched so a large document reports steady progress instead of sitting
    # at one percentage for a minute.
    update_document_progress(document_id, "embedding", 45,
                             page_count=page_count, chunk_count=len(chunks))

    batch_size = max(1, settings.embed_batch_size)
    rows: list[dict] = []
    total = len(chunks)

    for start in range(0, total, batch_size):
        batch = chunks[start:start + batch_size]
        vector_ids = add_chunks(document_id, batch)
        rows.extend(
            {"vector_id": vid, "chunk_text": c.text, "chunk_metadata": c.metadata}
            for vid, c in zip(vector_ids, batch)
        )
        done = min(start + batch_size, total)
        # 45 → 88 across the batches.
        progress = 45 + int(43 * done / total)
        update_document_progress(document_id, "embedding", progress,
                                 page_count=page_count, chunk_count=total)

    # ── indexing ────────────────────────────────────────────────────────
    update_document_progress(document_id, "indexing", 90,
                             page_count=page_count, chunk_count=total)
    save_chunks(document_id, rows)

    # A new document changes what retrieval returns, so any answer cached
    # against this corpus is now suspect.
    cache.invalidate_documents([document_id])

    # ── ready ───────────────────────────────────────────────────────────
    update_document_progress(
        document_id, "ready", 100,
        page_count=page_count, chunk_count=total, token_count=token_count,
    )
    log.info("Ingested %s (%s): %d chunks, %d tokens", filename, document_id, total, token_count)


def remove(document_id: str) -> None:
    """Delete a document and everything derived from it."""
    from app.db import delete_document

    vector_ids = get_document_vector_ids(document_id)
    if vector_ids:
        delete_vectors(vector_ids)
    cache.invalidate_documents([document_id])
    storage.delete(document_id)
    delete_document(document_id)
