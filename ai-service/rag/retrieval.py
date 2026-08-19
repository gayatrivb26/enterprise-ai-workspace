"""
rag/retrieval.py

The RAG query flow: question -> embed -> Chroma similarity search -> diversify
-> build a citation-friendly prompt -> stream the answer.

Three deliberate quality choices beyond "top-k and hope":

  1. **Scoping.** `document_ids` becomes a real `where` filter on the vector
     store, so "chat with these documents" constrains retrieval itself rather
     than filtering results after the fact.
  2. **Diversity (MMR).** Plain top-k frequently returns five near-duplicate
     chunks from the same section, which burns context without adding
     information. Maximal Marginal Relevance trades a little similarity for
     coverage.
  3. **A distance floor.** Chunks that aren't actually close to the question
     are dropped instead of padded into the prompt, so an unanswerable
     question produces "the sources don't cover this" rather than a confident
     answer assembled from noise.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Iterator, Sequence

from app.config import settings
from app.llm_client import strip_json_fences
from app.llm_service import StreamResult, complete
from app.llm_service import stream_answer as llm_stream
from rag.embedding import embed_texts, keyword_search, similarity_search

log = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = (
    "You are Folio, an internal knowledge assistant for a company workspace.\n"
    "\n"
    "Choose how to answer based on what was asked:\n"
    "\n"
    "1. GREETINGS AND SMALL TALK (\"hi\", \"thanks\", \"what can you do?\"): "
    "reply naturally and briefly, like a normal assistant. Do NOT cite "
    "anything and do NOT mention the sources — they are irrelevant here.\n"
    "\n"
    "2. QUESTIONS ABOUT THE USER'S DOCUMENTS: answer using ONLY the supplied "
    "sources, and cite every factual claim inline as [Source 1]. If the "
    "sources genuinely do not cover it, say so plainly and say what is "
    "missing. Never guess, and never pad an answer with unrelated sources.\n"
    "\n"
    "3. GENERAL-KNOWLEDGE QUESTIONS clearly unrelated to the documents "
    "(definitions, concepts, how something works): you MAY answer from your "
    "own knowledge. Say briefly and up front that this is general knowledge "
    "rather than something from their documents, then answer properly. Do "
    "not cite sources for this.\n"
    "\n"
    "4. If the sources disagree with each other, surface the disagreement "
    "instead of silently picking one.\n"
    "\n"
    "Style: short paragraphs, bold key terms, bullet lists where they help. "
    "Use Markdown. Keep the answer as short as the question allows."
)


@dataclass
class RetrievedChunk:
    id: str
    text: str
    source_path: str
    page: int | None
    heading: str | None
    distance: float
    document_id: str | None = None

    @property
    def relevance(self) -> float:
        """Distance mapped to a 0–1 score for display."""
        return round(max(0.0, min(1.0, 1.0 - self.distance / 2.0)), 3)

    def as_source(self, index: int) -> dict[str, Any]:
        return {
            "index": index,
            "chunk_id": self.id,
            "document_id": self.document_id,
            "source_path": self.source_path,
            "page": self.page,
            "heading": self.heading,
            "relevance": self.relevance,
            # A short preview so the UI can show what was actually cited
            # without a second round trip.
            "preview": _preview(self.text),
        }


def _preview(text: str, limit: int = 320) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _mmr(query_vec: Sequence[float], hits: list[dict], keep: int, lambda_: float) -> list[dict]:
    """
    Greedy Maximal Marginal Relevance over candidates that carry embeddings.

    Hits without an embedding — keyword matches, which were never sent through
    the model — cannot participate in a cosine comparison, but they must not be
    silently discarded either: they are precisely the results hybrid search was
    added to surface. They are re-merged afterwards in fusion order.
    """
    usable = [h for h in hits if h.get("embedding")]
    unembedded = [h for h in hits if not h.get("embedding")]

    if len(usable) <= 1 or not query_vec:
        return hits[:keep]

    selected: list[dict] = []
    candidates = list(usable)

    while candidates and len(selected) < keep:
        best, best_score = None, -math.inf
        for c in candidates:
            sim_to_query = _cosine(query_vec, c["embedding"])
            sim_to_selected = max(
                (_cosine(c["embedding"], s["embedding"]) for s in selected), default=0.0
            )
            score = lambda_ * sim_to_query - (1.0 - lambda_) * sim_to_selected
            if score > best_score:
                best, best_score = c, score
        selected.append(best)
        candidates.remove(best)

    # Interleave the literal matches back in, best-fused first, without
    # exceeding the window.
    if unembedded:
        room = max(0, keep - len(selected))
        if room:
            selected.extend(unembedded[:room])
        else:
            # The window is full of semantic hits; give the strongest literal
            # match a slot, since an exact term match is rarely noise.
            selected[-1] = unembedded[0]

    return selected[:keep]


def _scope_filter(document_ids: Sequence[str] | None) -> dict | None:
    ids = [d for d in (document_ids or []) if d]
    if not ids:
        return None
    if len(ids) == 1:
        return {"document_id": ids[0]}
    return {"document_id": {"$in": list(ids)}}


# Words that carry no retrieval signal. Kept small on purpose: over-filtering
# strips the very terms ("leave", "notice") that make a keyword hit useful.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "for",
    "with", "at", "by", "from", "is", "are", "was", "were", "be", "been", "am",
    "do", "does", "did", "have", "has", "had", "can", "could", "should",
    "would", "will", "shall", "may", "might", "must", "what", "which", "who",
    "whom", "whose", "when", "where", "why", "how", "this", "that", "these",
    "those", "it", "its", "as", "about", "into", "our", "their", "your", "my",
    "me", "we", "us", "you", "they", "them", "he", "she", "his", "her",
    "please", "tell", "give", "show", "explain", "summarise", "summarize",
    "there", "here", "any", "all", "some", "not", "no", "yes", "does",
}


def salient_terms(query: str, limit: int = 6) -> list[str]:
    """
    The words worth searching for literally: content words, longest first.

    Longest-first because a long token is far more likely to be the
    distinguishing one ("reimbursement" over "days"), and the keyword pass has
    a budget.
    """
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]{2,}", query or "")
    seen: dict[str, None] = {}
    for word in words:
        if word.lower() in _STOPWORDS:
            continue
        seen.setdefault(word, None)
    return sorted(seen, key=len, reverse=True)[:limit]


def _fuse(ranked_lists: list[list[dict]], k: int = 60) -> list[dict]:
    """
    Reciprocal Rank Fusion.

    Vector distance and keyword matching are not on a comparable scale — one is
    a float, the other is a yes/no — so they cannot simply be added. RRF only
    uses each item's *rank* within its own list, which sidesteps that entirely
    and is why it is the standard way to combine heterogeneous retrievers.
    """
    scores: dict[str, float] = {}
    merged: dict[str, dict] = {}

    for ranked in ranked_lists:
        for rank, hit in enumerate(ranked):
            chunk_id = hit["id"]
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
            # Prefer the copy carrying an embedding/distance, for MMR later.
            if chunk_id not in merged or (hit.get("embedding") and not merged[chunk_id].get("embedding")):
                merged[chunk_id] = {**merged.get(chunk_id, {}), **hit}

    for chunk_id, score in scores.items():
        merged[chunk_id]["fusion_score"] = score

    return sorted(merged.values(), key=lambda h: h["fusion_score"], reverse=True)


def _within_relevance_band(hits: list[dict]) -> list[dict]:
    """
    Keep hits close to the best one, rather than closer than a fixed number.

    An absolute cutoff is brittle — distances shift with the model, the chunk
    length and the phrasing — so the ceiling is anchored to the best hit for
    *this* query. The margin is additive rather than a multiple: multiplying a
    mediocre best distance produces a band wide enough to admit the entire
    corpus.
    """
    scored = [h for h in hits if h.get("distance") is not None]
    if not scored:
        return hits

    best = min(h["distance"] for h in scored)
    ceiling = best + settings.relevance_margin

    kept = []
    for hit in hits:
        # Keyword hits have no distance — they matched literally, which is a
        # stronger signal than proximity, so they are never filtered out here.
        if hit.get("distance") is None or hit["distance"] <= ceiling:
            kept.append(hit)
    return kept or hits[:1]


def retrieve(
    query: str,
    top_k: int | None = None,
    where: dict | None = None,
    rerank: bool = False,
    document_ids: Sequence[str] | None = None,
) -> list[RetrievedChunk]:
    """
    Hybrid retrieval: semantic nearest-neighbour search fused with literal
    keyword lookup, then diversified.

    Vector-only search was the cause of the most annoying failure mode this
    system had — a fact sitting verbatim in an indexed document that the
    assistant reported as "not mentioned", because the embedding of the
    question and the embedding of the passage did not land close enough.
    """
    k = top_k or settings.retrieval_top_k
    candidate_k = max(settings.rerank_candidate_k, k * 3)
    scope = where or _scope_filter(document_ids)

    vector_hits = similarity_search(
        query, top_k=candidate_k, where=scope, include_embeddings=True
    )

    keyword_hits: list[dict] = []
    try:
        keyword_hits = keyword_search(salient_terms(query), top_k=k * 2, where=scope)
    except Exception as e:
        # Keyword search is an enhancement; losing it must not lose the answer.
        log.warning("Keyword search failed (%s); using vector results only.", e)

    if not vector_hits and not keyword_hits:
        return []

    fused = _fuse([vector_hits, keyword_hits]) if keyword_hits else vector_hits
    near = _within_relevance_band(fused)

    if rerank and len(near) > k:
        near = _rerank(query, near, keep=k)
    else:
        query_vec = (embed_texts([query]) or [[]])[0]
        near = _mmr(query_vec, near, keep=k, lambda_=settings.mmr_lambda)

    return [
        RetrievedChunk(
            id=h["id"],
            text=h["text"],
            source_path=h["metadata"].get("source_path", "unknown"),
            page=h["metadata"].get("page"),
            heading=h["metadata"].get("heading"),
            # A literal-only hit has no measured distance. Report it as a
            # moderate match rather than inventing a strong one — it was found
            # by containing a word, which is weaker evidence than proximity.
            distance=h["distance"] if h.get("distance") is not None else 0.95,
            document_id=h["metadata"].get("document_id"),
        )
        for h in near
    ]


def _rerank(query: str, hits: list[dict], keep: int) -> list[dict]:
    """
    Cheap LLM-based re-ranker (Phase 3). A cross-encoder (e.g.
    ms-marco-MiniLM) would be cheaper/faster in production; this version
    uses a single Claude call scoring all candidates so you can see the
    technique before optimizing it.
    """
    numbered = "\n\n".join(f"[{i}] {h['text'][:500]}" for i, h in enumerate(hits))
    prompt = (
        f"Question: {query}\n\n"
        f"Below are {len(hits)} candidate passages. Return ONLY a JSON array "
        f"of the {keep} indices (integers) most relevant to answering the "
        f"question, ordered best first. No other text.\n\n{numbered}"
    )
    import json

    try:
        response_text, _ = complete(prompt, max_tokens=200, operation="rerank")
        indices = json.loads(strip_json_fences(response_text))
        picked = [hits[i] for i in indices if isinstance(i, int) and 0 <= i < len(hits)][:keep]
        return picked or hits[:keep]
    except Exception:
        # If the model output isn't clean JSON, fail open to plain top-k.
        return hits[:keep]


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------

def build_prompt(
    question: str,
    chunks: list[RetrievedChunk],
    memory_context: str = "",
    history: Sequence[dict[str, str]] = (),
) -> str:
    if not chunks:
        sources_block = "(No documents matched this question.)"
    else:
        sources_block = "\n\n".join(
            f"[Source {i + 1}] {c.source_path}"
            f"{f', page {c.page}' if c.page else ''}"
            f"{f', section: {c.heading}' if c.heading else ''}\n{c.text}"
            for i, c in enumerate(chunks)
        )

    parts: list[str] = []

    if memory_context:
        parts.append(f"What you remember about this user:\n{memory_context}")

    if history:
        # Recent turns let follow-ups like "and what about contractors?"
        # resolve. Trimmed hard so history never crowds out the sources.
        rendered = "\n".join(
            f"{'User' if h.get('role') == 'user' else 'Assistant'}: "
            f"{_preview(h.get('content', ''), 400)}"
            for h in history
        )
        parts.append(f"Earlier in this conversation:\n{rendered}")

    parts.append(f"SOURCES\n{sources_block}")
    parts.append(f"QUESTION\n{question}")
    parts.append(
        "Answer using only the sources above, citing them inline as [Source N]. "
        "If they do not contain the answer, say so."
    )

    return "\n\n---\n\n".join(parts)


def stream_answer(
    question: str,
    chunks: list[RetrievedChunk],
    memory_context: str = "",
    history: Sequence[dict[str, str]] = (),
    *,
    cache_key: str | None = None,
    user_id: str | None = None,
    chat_id: str | None = None,
    result: StreamResult | None = None,
) -> Iterator[str]:
    """Yields text deltas for SSE streaming back through ASP.NET Core."""
    prompt = build_prompt(question, chunks, memory_context, history)
    document_ids = [c.document_id for c in chunks if c.document_id]
    sources = [c.as_source(i + 1) for i, c in enumerate(chunks)]

    return llm_stream(
        prompt,
        cache_key=cache_key,
        cache_document_ids=document_ids,
        sources=sources,
        system_instruction=SYSTEM_INSTRUCTION,
        operation="chat",
        user_id=user_id,
        chat_id=chat_id,
        result=result,
    )
