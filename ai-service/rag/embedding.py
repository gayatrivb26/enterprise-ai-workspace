"""
rag/embedding.py

Owns the Chroma collection and the embedding calls. Kept deliberately swappable:
today it uses Google Gemini for generation but a local sentence-transformers
model for embeddings (kept local/free regardless of LLM provider), so this
is the one place you'd change if you moved to Voyage AI / OpenAI embeddings later.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import chromadb
from chromadb.utils import embedding_functions

from app.config import settings

log = logging.getLogger(__name__)

_client: chromadb.HttpClient | None = None
_collection = None
_embedding_fn = None

COLLECTION_NAME = "document_chunks"

# A term matching more chunks than this is too common to identify anything.
MAX_HITS_PER_TERM = 4

# Ceiling on literal-only results, so a keyword pass can supplement the
# semantic ranking without ever replacing it.
MAX_KEYWORD_ONLY = 4


def _get_embedding_fn():
    # Loaded lazily (not at import time) so that app startup and /health
    # never block on the model download. The all-MiniLM-L6-v2 weights
    # (~90MB) download from Hugging Face on first call and are cached
    # inside the container filesystem — since that's not bind-mounted,
    # the cache is lost on every container rebuild, so expect a one-time
    # delay after each `docker compose up --build`.
    global _embedding_fn
    if _embedding_fn is None:
        log.info("Loading sentence-transformers model (first use only)...")
        _embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
        log.info("Embedding model ready.")
    return _embedding_fn


def get_collection():
    global _client, _collection
    if _collection is None:
        _client = chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)
        _collection = _client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=_get_embedding_fn(),
        )
    return _collection


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed arbitrary text with the same model used for the corpus."""
    if not texts:
        return []
    return list(_get_embedding_fn()(texts))


def add_chunks(document_id: str, chunks: list[Any]) -> list[str]:
    """chunks: list of rag.chunking.Chunk. Returns the vector_ids assigned."""
    if not chunks:
        return []
    collection = get_collection()
    ids = [str(uuid.uuid4()) for _ in chunks]
    documents = [c.text for c in chunks]
    metadatas = [
        {"document_id": document_id, **{k: v for k, v in c.metadata.items() if v is not None}}
        for c in chunks
    ]
    collection.add(ids=ids, documents=documents, metadatas=metadatas)
    return ids


def delete_vectors(vector_ids: list[str]) -> None:
    """Remove specific vectors — used when a document is deleted or re-ingested."""
    if not vector_ids:
        return
    try:
        get_collection().delete(ids=vector_ids)
    except Exception as e:
        log.warning("Failed to delete %d vectors: %s", len(vector_ids), e)


def delete_document_vectors(document_id: str) -> None:
    """Fallback path: delete by metadata filter rather than by id list."""
    try:
        get_collection().delete(where={"document_id": document_id})
    except Exception as e:
        log.warning("Failed to delete vectors for document %s: %s", document_id, e)


def similarity_search(
    query: str,
    top_k: int,
    where: dict | None = None,
    include_embeddings: bool = False,
) -> list[dict]:
    """
    Nearest-neighbour search. `where` scopes the search — passing
    {"document_id": {"$in": [...]}} is what makes "chat with these documents"
    a real retrieval constraint rather than a UI-only filter.
    """
    collection = get_collection()
    include = ["documents", "metadatas", "distances"]
    if include_embeddings:
        include.append("embeddings")

    results = collection.query(
        query_texts=[query],
        n_results=max(1, top_k),
        where=where or None,
        include=include,
    )

    ids = results.get("ids") or [[]]
    if not ids or not ids[0]:
        return []

    embeddings = None
    if include_embeddings:
        raw = results.get("embeddings")
        if raw is not None and len(raw) > 0 and raw[0] is not None:
            embeddings = raw[0]

    hits: list[dict] = []
    for i, cid in enumerate(ids[0]):
        hit = {
            "id": cid,
            "text": results["documents"][0][i],
            "metadata": results["metadatas"][0][i] or {},
            "distance": results["distances"][0][i],
        }
        if embeddings is not None:
            hit["embedding"] = list(embeddings[i])
        hits.append(hit)
    return hits


def keyword_search(
    terms: list[str],
    top_k: int,
    where: dict | None = None,
) -> list[dict]:
    """
    Literal substring lookup over chunk text, with no embedding involved.

    This exists because vector search alone genuinely misses things. Embeddings
    compress meaning, so a rare token — a policy number, a product codename, an
    unusual surname — can sit *verbatim* in a chunk and still not surface,
    which is exactly the "but it says it right there" failure. Substring
    matching is precise where embeddings are fuzzy; used together they cover
    each other's blind spots.

    `collection.get` is used rather than `query` because there is no query
    vector here: this is a filter, not a nearest-neighbour search.
    """
    if not terms:
        return []

    collection = get_collection()
    found: dict[str, dict] = {}

    for term in terms:
        term = term.strip()
        if len(term) < 3:
            continue

        # Selectivity test, in the spirit of inverse document frequency.
        #
        # A literal match is only evidence when the term is *distinctive*. A
        # common word matches everywhere: searching "leave" for a leave-policy
        # question pulled in seven passages from a novel about someone leaving
        # a desert, and because keyword hits skip the distance filter they
        # outranked the actual policy. If a term matches more chunks than this,
        # it is not identifying anything and its hits are discarded.
        try:
            probe = collection.get(
                where=where or None,
                where_document={"$contains": term},
                limit=MAX_HITS_PER_TERM + 1,
                include=[],
            )
            if len((probe.get("ids") or [])) > MAX_HITS_PER_TERM:
                log.debug("Term %r matches too broadly to be selective; skipping.", term)
                continue
        except Exception as e:
            log.debug("Selectivity probe for %r failed: %s", term, e)
            continue

        # Chroma's $contains is case-sensitive, so try the variants a document
        # is actually likely to use rather than assuming one casing.
        variants = {term, term.lower(), term.capitalize()}
        for variant in variants:
            if len(found) >= top_k:
                break
            try:
                result = collection.get(
                    where=where or None,
                    where_document={"$contains": variant},
                    limit=top_k,
                    include=["documents", "metadatas"],
                )
            except Exception as e:
                log.debug("Keyword lookup for %r failed: %s", variant, e)
                continue

            ids = result.get("ids") or []
            documents = result.get("documents") or []
            metadatas = result.get("metadatas") or []

            for index, chunk_id in enumerate(ids):
                if chunk_id in found:
                    continue
                found[chunk_id] = {
                    "id": chunk_id,
                    "text": documents[index] if index < len(documents) else "",
                    "metadata": (metadatas[index] if index < len(metadatas) else {}) or {},
                    # No distance: this matched literally, not by proximity.
                    "distance": None,
                    "matched_term": term,
                }

    return list(found.values())[:MAX_KEYWORD_ONLY]
