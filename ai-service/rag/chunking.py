"""
rag/chunking.py

Chunking is the single biggest lever on RAG quality, so this deliberately does
NOT do naive fixed-token windows. Two strategies:

  - Markdown: split on heading boundaries first (so a chunk never straddles
    two unrelated sections), then sub-split any section that's still too long.
  - PDF: split on paragraph boundaries within a page, then merge small
    paragraphs together up to the token budget, carrying page number metadata.

Every chunk keeps enough chunk_metadata to support the Phase 3 hybrid filters
(e.g. "only search wiki pages tagged HR") and to render citations.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import tiktoken

_ENC = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_ENC.encode(text))


@dataclass
class Chunk:
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _split_by_token_budget(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    """Fallback splitter for any single block that's still too big."""
    tokens = _ENC.encode(text)
    if len(tokens) <= max_tokens:
        return [text]

    chunks = []
    start = 0
    while start < len(tokens):
        end = min(start + max_tokens, len(tokens))
        chunks.append(_ENC.decode(tokens[start:end]))
        if end == len(tokens):
            break
        start = end - overlap_tokens  # overlap so context isn't lost at the seam
    return chunks


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)


def chunk_markdown(
    text: str,
    source_path: str,
    max_tokens: int = 400,
    overlap_tokens: int = 60,
) -> list[Chunk]:
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        # No headings at all — treat the whole doc as one section.
        sections = [("", text)]
    else:
        sections = []
        for i, m in enumerate(matches):
            heading = m.group(2).strip()
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            sections.append((heading, text[start:end].strip()))
        # capture any preamble before the first heading
        if matches[0].start() > 0:
            preamble = text[: matches[0].start()].strip()
            if preamble:
                sections.insert(0, ("", preamble))

    chunks: list[Chunk] = []
    for heading, body in sections:
        if not body:
            continue
        sub_texts = _split_by_token_budget(body, max_tokens, overlap_tokens)
        for idx, sub in enumerate(sub_texts):
            chunks.append(
                Chunk(
                    text=(f"{heading}\n\n{sub}" if heading else sub),
                    metadata={
                        "source_path": source_path,
                        "heading": heading or None,
                        "part": idx + 1 if len(sub_texts) > 1 else None,
                    },
                )
            )
    return chunks


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def chunk_pdf(
    pages: list[str],
    source_path: str,
    max_tokens: int = 400,
    overlap_tokens: int = 60,
) -> list[Chunk]:
    """`pages` is a list of raw extracted text, one entry per PDF page."""
    chunks: list[Chunk] = []

    for page_num, page_text in enumerate(pages, start=1):
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", page_text) if p.strip()]
        if not paragraphs:
            continue

        buffer = ""
        for para in paragraphs:
            candidate = f"{buffer}\n\n{para}" if buffer else para
            if count_tokens(candidate) <= max_tokens:
                buffer = candidate
            else:
                if buffer:
                    chunks.append(
                        Chunk(text=buffer, metadata={"source_path": source_path, "page": page_num})
                    )
                # paragraph itself may exceed budget — hard split it
                for sub in _split_by_token_budget(para, max_tokens, overlap_tokens):
                    chunks.append(
                        Chunk(text=sub, metadata={"source_path": source_path, "page": page_num})
                    )
                buffer = ""
        if buffer:
            chunks.append(Chunk(text=buffer, metadata={"source_path": source_path, "page": page_num}))

    return chunks


# ---------------------------------------------------------------------------
# Structured blocks (Word / Excel / PowerPoint / PDF pages)
# ---------------------------------------------------------------------------

def chunk_blocks(
    blocks,
    source_path: str,
    max_tokens: int = 400,
    overlap_tokens: int = 60,
) -> list[Chunk]:
    """
    Chunk a list of rag.parsers.Block, merging small adjacent blocks that share
    the same structural context and hard-splitting any block that is too big.

    Merging matters: a spreadsheet row or a bullet line is far below the token
    budget on its own, and embedding hundreds of one-line chunks produces
    uniformly weak matches. Merging only within the same section/sheet/slide
    keeps a chunk from straddling two unrelated contexts.
    """
    chunks: list[Chunk] = []
    buffer: list[str] = []
    buffer_meta: dict | None = None

    def context_of(meta: dict) -> tuple:
        return (meta.get("page"), meta.get("sheet"), meta.get("heading"), meta.get("table"))

    def flush() -> None:
        nonlocal buffer, buffer_meta
        if not buffer:
            return
        text = "\n".join(buffer)
        meta = {"source_path": source_path, **{k: v for k, v in (buffer_meta or {}).items() if v is not None}}
        for part in _split_by_token_budget(text, max_tokens, overlap_tokens):
            chunks.append(Chunk(text=part, metadata=dict(meta)))
        buffer = []
        buffer_meta = None

    for block in blocks:
        text = (block.text or "").strip()
        if not text:
            continue

        if buffer_meta is not None and context_of(block.metadata) != context_of(buffer_meta):
            flush()

        candidate = "\n".join([*buffer, text])
        if buffer and count_tokens(candidate) > max_tokens:
            flush()

        buffer.append(text)
        if buffer_meta is None:
            buffer_meta = dict(block.metadata)

    flush()
    return chunks
