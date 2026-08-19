"""
rag/ingest.py

File parsing plus the legacy synchronous ingest helper.

The staged, observable pipeline the API actually uses now lives in
app/pipeline.py — it reports progress into the `documents` row so the UI can
show what is happening. `ingest_document` below is kept because the eval
harness and the agents call it directly and just want a blocking call.
"""
from __future__ import annotations

import fitz  # PyMuPDF

from app.config import settings
from rag.chunking import chunk_markdown, chunk_pdf
from rag.embedding import add_chunks


def parse_pdf_pages(file_bytes: bytes) -> list[str]:
    # Context-managed so the file handle is released even if extraction throws
    # — this runs in a long-lived worker, and leaked handles accumulate.
    with fitz.open(stream=file_bytes, filetype="pdf") as doc:
        return [page.get_text() for page in doc]


def parse_text(file_bytes: bytes) -> str:
    return file_bytes.decode("utf-8", errors="replace")


def ingest_document(document_id: str, filename: str, file_type: str, file_bytes: bytes) -> list[dict]:
    """
    Blocking ingest. Returns a list of {vector_id, chunk_text, chunk_metadata}
    dicts — the caller persists these to Postgres.
    """
    if file_type == "pdf":
        pages = parse_pdf_pages(file_bytes)
        chunks = chunk_pdf(
            pages,
            source_path=filename,
            max_tokens=settings.chunk_size_tokens,
            overlap_tokens=settings.chunk_overlap_tokens,
        )
    elif file_type in ("markdown", "text"):
        chunks = chunk_markdown(
            parse_text(file_bytes),
            source_path=filename,
            max_tokens=settings.chunk_size_tokens,
            overlap_tokens=settings.chunk_overlap_tokens,
        )
    else:
        raise ValueError(f"Unsupported file_type: {file_type}")

    if not chunks:
        return []

    vector_ids = add_chunks(document_id, chunks)
    return [
        {"vector_id": vid, "chunk_text": c.text, "chunk_metadata": c.metadata}
        for vid, c in zip(vector_ids, chunks)
    ]
