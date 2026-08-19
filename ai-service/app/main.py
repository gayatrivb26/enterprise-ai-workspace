"""
app/main.py — the FastAPI AI service entrypoint.

Endpoints
  POST   /documents               upload a file; ingestion runs in the background
  GET    /documents               list, with search / status / collection filters
  GET    /documents/{id}          one document, including live pipeline progress
  DELETE /documents/{id}          remove the document, its chunks and its vectors
  POST   /documents/{id}/reingest re-run the pipeline for an existing document
  GET    /documents/events        SSE stream of live pipeline progress
  GET    /collections             list collections
  POST   /collections             create a collection
  DELETE /collections/{id}        delete a collection
  POST   /chat/stream             RAG-grounded chat answer, streamed as SSE
  GET    /stats                   cache + token/cost telemetry
  GET    /health

A note on threading, because it caused a real bug: the LLM and embedding SDKs
are synchronous and blocking. Calling them directly from an `async def` pins
the single asyncio event loop for the whole generation, which serialises every
other request and stops SSE keep-alives and disconnect detection from running.
Every blocking call below therefore goes through `run_in_threadpool` /
`iterate_in_threadpool`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from agents import dev_agent, git_tools, meeting_agent
from app import cache, db, pipeline, storage
from memory import store as memory_store
from app.security import service_token_middleware
from app.config import settings
from app.llm_service import LlmError, StreamResult
from app.llm_service import stream_answer as llm_stream
from app.router import classify
from rag.retrieval import retrieve, stream_answer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    Warm the embedding model in the background at startup.

    The first call to sentence-transformers downloads (~90 MB) and loads the
    model, which can take tens of seconds. Without this that cost lands on
    whoever uploads first, so a 1 KB text file appears to hang at "Embedding"
    while the model loads. Doing it here moves the wait to boot, where nobody
    is watching a progress bar.

    Deliberately fire-and-forget on a worker thread: a slow or failed warmup
    must not stop the service from accepting requests, and the model still
    loads lazily on first use if this does not finish in time.
    """
    def warm() -> None:
        try:
            from rag.embedding import get_collection
            get_collection()
            log.info("Embedding model and vector store ready.")
        except Exception as e:
            log.warning("Could not warm the embedding model (%s); "
                        "it will load on first use instead.", e)

    threading.Thread(target=warm, name="embedding-warmup", daemon=True).start()
    yield


app = FastAPI(
    title="Enterprise AI Workspace — AI Service",
    lifespan=lifespan,
)

# Ordering matters: the auth check runs before anything touches the database
# or the model, so an unauthenticated caller costs nothing.
app.middleware("http")(service_token_middleware)

app.add_middleware(
    CORSMiddleware,
    # This service is called server-to-server by the ASP.NET API, never by a
    # browser, so no origin needs cross-origin access to it.
    allow_origins=[],
    allow_methods=["POST", "GET", "DELETE"],
    allow_headers=["X-Service-Token", "Content-Type"],
)


@app.get("/health")
def health():
    return {"status": "ok", "model": settings.gemini_model, "prompt_version": settings.prompt_version}


@app.get("/stats")
async def stats(user_id: str = Query(...)):
    usage = await run_in_threadpool(db.usage_summary, user_id)
    return {"cache": cache.stats(), "usage": usage}


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

def _serialize_document(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "user_id": str(row["user_id"]),
        "filename": row["filename"],
        "type": row["type"],
        "status": row["status"],
        "progress": row.get("progress", 0),
        "error": row.get("error"),
        "size_bytes": row.get("size_bytes", 0),
        "page_count": row.get("page_count"),
        "chunk_count": row.get("chunk_count", 0),
        "token_count": row.get("token_count", 0),
        "collection_id": str(row["collection_id"]) if row.get("collection_id") else None,
        "uploaded_at": row["uploaded_at"].isoformat() if row.get("uploaded_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


@app.post("/documents")
async def upload_document(
    background: BackgroundTasks,
    user_id: str = Form(...),
    file: UploadFile = File(...),
    collection_id: str | None = Form(None),
):
    """
    Accepts the file, records it as `queued`, and returns immediately. The
    client then follows progress via /documents/events or by polling.
    """
    filename = file.filename or "untitled"
    file_type = pipeline.detect_type(filename)
    if file_type is None:
        raise HTTPException(400, "Only .pdf, .md and .txt files are supported.")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(400, "The uploaded file is empty.")
    if len(file_bytes) > settings.max_upload_bytes:
        raise HTTPException(
            413, f"File exceeds the {settings.max_upload_bytes // (1024 * 1024)} MB limit."
        )

    digest = pipeline.content_hash(file_bytes)

    # Re-uploading a byte-identical file is common (people re-drag the same
    # folder). Point at the existing document instead of embedding it twice.
    duplicate = await run_in_threadpool(db.find_duplicate, user_id, digest)
    if duplicate:
        return {
            "document_id": str(duplicate["id"]),
            "status": duplicate["status"],
            "duplicate_of": str(duplicate["id"]),
            "message": "This file is already indexed.",
        }

    document_id = await run_in_threadpool(
        db.create_document, user_id, filename, file_type,
        len(file_bytes), digest, collection_id or None,
    )

    # Retained so a failed ingest can genuinely be retried later.
    await run_in_threadpool(storage.save, document_id, filename, file_bytes)

    background.add_task(pipeline.ingest, document_id, filename, file_type, file_bytes)

    return {"document_id": document_id, "status": "queued", "progress": 0}


@app.get("/documents")
async def list_documents(
    user_id: str = Query(...),
    search: str | None = Query(None),
    status: str | None = Query(None),
    collection_id: str | None = Query(None),
):
    rows = await run_in_threadpool(db.list_documents, user_id, search, status, collection_id)
    return {"documents": [_serialize_document(r) for r in rows]}


@app.get("/documents/events")
async def document_events(user_id: str = Query(...)):
    """
    Live pipeline progress as SSE.

    Postgres is polled rather than using a pub/sub channel so this keeps
    working when ingestion runs in a different process (or after a restart),
    and needs no extra infrastructure. Only genuine changes are emitted, so an
    idle workspace costs one cheap query per tick and no traffic.
    """
    async def generator():
        seen: dict[str, tuple[str, int]] = {}
        # Prime with current state so a late subscriber isn't left blank.
        first = True
        while True:
            try:
                rows = await run_in_threadpool(db.list_documents, user_id, None, None, None)
            except Exception as e:
                log.warning("Document event poll failed: %s", e)
                await asyncio.sleep(2.0)
                continue

            changed = []
            active = 0
            for row in rows:
                doc_id = str(row["id"])
                state = (row["status"], row.get("progress", 0))
                if row["status"] not in ("ready", "failed"):
                    active += 1
                if first or seen.get(doc_id) != state:
                    changed.append(_serialize_document(row))
                seen[doc_id] = state

            # Documents deleted elsewhere should disappear from the client too.
            live_ids = {str(r["id"]) for r in rows}
            removed = [d for d in seen.keys() - live_ids]
            for doc_id in removed:
                seen.pop(doc_id, None)

            if changed or removed:
                yield {
                    "event": "documents",
                    "data": json.dumps({"changed": changed, "removed": removed}),
                }

            first = False
            # Poll briskly while work is in flight, lazily when idle.
            await asyncio.sleep(0.6 if active else 3.0)

    return EventSourceResponse(generator())


@app.get("/documents/{document_id}")
async def get_document(document_id: str):
    row = await run_in_threadpool(db.get_document, document_id)
    if row is None:
        raise HTTPException(404, "Document not found.")
    return _serialize_document(row)


@app.delete("/documents/{document_id}")
async def delete_document(document_id: str):
    row = await run_in_threadpool(db.get_document, document_id)
    if row is None:
        raise HTTPException(404, "Document not found.")
    await run_in_threadpool(pipeline.remove, document_id)
    return {"deleted": document_id}


@app.post("/documents/{document_id}/reingest")
async def reingest_document(document_id: str, background: BackgroundTasks):
    """Retry ingestion from the stored source file."""
    row = await run_in_threadpool(db.get_document, document_id)
    if row is None:
        raise HTTPException(404, "Document not found.")
    if not storage.exists(document_id):
        raise HTTPException(
            409, "The original file is no longer available. Please upload it again."
        )

    await run_in_threadpool(db.update_document_progress, document_id, "queued", 0)
    background.add_task(pipeline.reingest, document_id)
    return {"document_id": document_id, "status": "queued", "progress": 0}


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

class CollectionRequest(BaseModel):
    user_id: str
    name: str = Field(min_length=1, max_length=80)
    color: str = "indigo"


@app.get("/collections")
async def list_collections(user_id: str = Query(...)):
    rows = await run_in_threadpool(db.list_collections, user_id)
    return {
        "collections": [
            {
                "id": str(r["id"]),
                "name": r["name"],
                "color": r["color"],
                "document_count": r["document_count"],
            }
            for r in rows
        ]
    }


@app.post("/collections")
async def create_collection(req: CollectionRequest):
    row = await run_in_threadpool(db.create_collection, req.user_id, req.name.strip(), req.color)
    return {"id": str(row["id"]), "name": row["name"], "color": row["color"], "document_count": 0}


@app.delete("/collections/{collection_id}")
async def delete_collection(collection_id: str):
    await run_in_threadpool(db.delete_collection, collection_id)
    return {"deleted": collection_id}


def _has_ready_documents(user_id: str) -> bool:
    try:
        return any(d["status"] == "ready" for d in db.list_documents(user_id, None, "ready", None))
    except Exception:
        return True  # assume yes: routing to RAG is the safe default


async def _stream_dev_agent(
    question: str,
    routing,
    github_token: str | None = None,
    github_repo: str | None = None,
) -> Any:
    """
    Run the Dev Agent and emit its answer over the same SSE contract.

    The agent is not token-streaming (it runs tools, then writes once), so the
    finished answer is replayed in small pieces. The UI should not have to know
    which capability answered it.
    """
    yield {"event": "sources", "data": json.dumps([])}
    yield {"event": "route", "data": json.dumps(
        {"intent": "code", "reason": routing.reason})}

    outcome = await run_in_threadpool(dev_agent.run, question, github_token, github_repo)
    answer = outcome.get("answer") or "The Dev Agent returned nothing."

    for piece in _chunk_text(answer):
        yield {"event": "delta", "data": piece}

    yield {"event": "meta", "data": json.dumps({
        "cached": False,
        "grounded": bool(outcome.get("tools_used")),
        "chunks": 0,
        "intent": "code",
        "tools_used": outcome.get("tools_used", []),
        "source": outcome.get("source"),
    })}
    yield {"event": "done", "data": ""}


async def _stream_meeting_agent(text: str, user_id: str, routing) -> Any:
    """Summarise a transcript and persist its action items as memory."""
    yield {"event": "sources", "data": json.dumps([])}
    yield {"event": "route", "data": json.dumps(
        {"intent": "meeting", "reason": routing.reason})}

    outcome = await run_in_threadpool(meeting_agent.run, text, user_id)
    answer = outcome.get("markdown") or "I could not summarise that transcript."

    for piece in _chunk_text(answer):
        yield {"event": "delta", "data": piece}

    yield {"event": "meta", "data": json.dumps({
        "cached": False,
        "grounded": True,
        "chunks": 0,
        "intent": "meeting",
        "action_items": len(outcome.get("action_items", [])),
    })}
    yield {"event": "done", "data": ""}


def _chunk_text(text: str, size: int = 28):
    """Word-aligned pieces so a non-streaming answer still types out."""
    i, n = 0, len(text)
    while i < n:
        end = min(i + size, n)
        if end < n:
            space = text.rfind(" ", i + 1, end + 1)
            if space > i:
                end = space + 1
        yield text[i:end]
        i = end


from app.persona import ASSISTANT_IDENTITY


async def _stream_general_chat(req: "ChatRequest", routing) -> Any:
    """
    Answer without touching retrieval.

    Greetings, questions about Folio itself, and general knowledge have
    nothing to do with the corpus. Sending them through RAG was actively
    harmful: it put unrelated passages in front of a grounded model, which
    then refused to answer, or described what those passages did not say.
    """
    yield {"event": "sources", "data": json.dumps([])}

    turns = [
        ("User" if h.role == "user" else "Assistant") + ": " + h.content[:600]
        for h in req.history[-6:]
    ]
    history = chr(10).join(turns)
    prefix = ("Earlier in this conversation:" + chr(10) + history + chr(10) * 2) if turns else ""
    prompt = prefix + req.question.strip()

    result = StreamResult()
    try:
        generator = llm_stream(
            prompt,
            system_instruction=ASSISTANT_IDENTITY,
            operation="chat",
            user_id=req.user_id,
            chat_id=req.chat_id,
            result=result,
            temperature=0.4,   # a little warmth; this is conversation
        )
        async for delta in iterate_in_threadpool(generator):
            yield {"event": "delta", "data": delta}
    except LlmError as e:
        log.error("General chat failed: %s", e)
        yield {"event": "error", "data": e.user_message}
        yield {"event": "done", "data": ""}
        return

    yield {"event": "meta", "data": json.dumps({
        "cached": result.usage.cache_hit,
        "grounded": False,
        "chunks": 0,
        "intent": "chat",
        "tokens_in": result.usage.tokens_in,
        "tokens_out": result.usage.tokens_out,
    })}
    yield {"event": "done", "data": ""}


# ---------------------------------------------------------------------------
# Chat (RAG)
# ---------------------------------------------------------------------------

class HistoryTurn(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    user_id: str
    question: str
    use_memory: bool = True
    chat_id: str | None = None
    document_ids: list[str] = Field(default_factory=list)
    history: list[HistoryTurn] = Field(default_factory=list)
    # Supplied by the ASP.NET API only when the user has connected GitHub and
    # picked a repository. Never logged, never persisted, never echoed back.
    github_token: str | None = None
    github_repo: str | None = None


# Matches the citation markers the prompt asks the model to emit.
_CITATION_RE = re.compile(r"\[Source\s+(\d+)\]", re.IGNORECASE)


def _infer_document_scope(user_id: str, question: str) -> list[str]:
    """
    If the question names one of the user's documents, scope retrieval to it.

    Pure vector similarity cannot do this: asking to "summarise genpact qn.txt"
    embeds a sentence about a *filename*, which is nowhere in that file's text,
    so a single 271-byte chunk loses to hundreds of chunks from a large PDF and
    the assistant reports the file as missing while it sits there indexed.
    Matching the name the user actually typed fixes the case they care about
    most — talking about one specific document.
    """
    try:
        docs = db.list_documents(user_id, None, "ready", None)
    except Exception as e:
        log.warning("Could not resolve document names: %s", e)
        return []

    lowered = question.lower()
    matched: list[str] = []
    for doc in docs:
        filename = str(doc["filename"])
        stem = filename.rsplit(".", 1)[0]
        # Require a reasonably distinctive stem so a file called "a.txt"
        # doesn't capture every question containing the letter "a".
        if filename.lower() in lowered or (len(stem) >= 4 and stem.lower() in lowered):
            matched.append(str(doc["id"]))

    return matched


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "Question must not be empty.")

    async def event_generator():
        result = StreamResult()
        try:
            # ── Route first ────────────────────────────────────────────
            # Deciding what the message *is* before doing any work avoids
            # embedding a greeting and avoids sending a repository question
            # to document retrieval, which could only ever answer it badly.
            has_documents = await run_in_threadpool(_has_ready_documents, req.user_id)
            # A connected GitHub repo counts as a repository just as much as a
            # locally mounted checkout does.
            has_repository = bool(req.github_token and req.github_repo) or git_tools.repo_available()

            routing = await run_in_threadpool(
                classify,
                question,
                has_documents=has_documents,
                has_repository=has_repository,
            )
            log.info("Routed to %s (%s)", routing.intent, routing.reason)
            # Emitted up front so the UI can describe what it is doing while it
            # happens, rather than guessing until the answer lands.
            yield {"event": "route", "data": json.dumps(
                {"intent": routing.intent, "reason": routing.reason})}

            if routing.intent == "code":
                async for frame in _stream_dev_agent(
                    question, routing, req.github_token, req.github_repo
                ):
                    yield frame
                return

            if routing.intent == "meeting":
                async for frame in _stream_meeting_agent(question, req.user_id, routing):
                    yield frame
                return

            if routing.intent == "chat":
                # No retrieval at all. Running it for "hi" surfaced a
                # "Searching your documents" step for a greeting, and — worse —
                # put unrelated passages in front of a grounded model, which
                # then answered "who built you?" by reporting what a novel in
                # the context did not say.
                async for frame in _stream_general_chat(req, routing):
                    yield frame
                return

            # An explicit UI selection always wins; otherwise fall back to any
            # document the question names by filename.
            scope = req.document_ids or await run_in_threadpool(
                _infer_document_scope, req.user_id, question
            )
            # A scoped question is usually "tell me about *this* document", so
            # widen the window — the whole corpus isn't competing for slots.
            top_k = settings.retrieval_top_k * 2 if scope else settings.retrieval_top_k

            # --- Retrieval (blocking: embeddings + vector search) ---------
            chunks = await run_in_threadpool(
                retrieve, question, top_k, None, False, scope
            )

            memory_context = ""
            if req.use_memory:
                # Relevance-ranked, not merely recent: the newest thing a user
                # committed to is usually not what they are now asking about.
                memories = await run_in_threadpool(
                    memory_store.get_relevant_memory, req.user_id, question
                )
                memory_context = "\n".join(f"- {m}" for m in memories)
                if memories:
                    # Feeds the recency term next time these are ranked.
                    await run_in_threadpool(memory_store.touch, req.user_id, memories)

            sources = [c.as_source(i + 1) for i, c in enumerate(chunks)]

            # Emit the retrieved set up front so the UI can show progress; it is
            # replaced below by the subset the answer actually cited.
            yield {"event": "sources", "data": json.dumps(sources)}

            # --- Cache key over everything that shaped this answer ---------
            key = cache.answer_key(
                user_id=req.user_id,
                question=question,
                document_ids=[c.document_id for c in chunks if c.document_id],
                chunk_ids=[c.id for c in chunks],
                model=settings.gemini_model,
                prompt_version=settings.prompt_version,
                memory_fingerprint=cache.fingerprint([memory_context]),
            )

            history = [{"role": h.role, "content": h.content} for h in req.history][-6:]

            generator = stream_answer(
                question, chunks, memory_context, history,
                cache_key=key, user_id=req.user_id, chat_id=req.chat_id, result=result,
            )

            # iterate_in_threadpool keeps the blocking SDK off the event loop.
            async for delta in iterate_in_threadpool(generator):
                yield {"event": "delta", "data": delta}

            # Show only what the answer actually leaned on. Retrieval always
            # returns its nearest neighbours, so a greeting or a general
            # question would otherwise arrive decorated with citations it
            # never used — which reads as though the reply came from a
            # document when it did not.
            cited = {int(n) for n in _CITATION_RE.findall(result.text)}
            grounded = bool(cited)
            yield {
                "event": "sources",
                "data": json.dumps([s for s in sources if s["index"] in cited]),
            }

            yield {"event": "meta", "data": json.dumps({
                "cached": result.usage.cache_hit,
                "grounded": grounded,
                "chunks": len(chunks),
                "model": result.usage.model,
                "tokens_in": result.usage.tokens_in,
                "tokens_out": result.usage.tokens_out,
                "cost_usd": result.usage.cost_usd,
                "latency_ms": result.usage.latency_ms,
            })}
            yield {"event": "done", "data": ""}

        except LlmError as e:
            log.error("LLM failed for chat %s: %s", req.chat_id, e)
            yield {"event": "error", "data": e.user_message}
            yield {"event": "done", "data": ""}
        except Exception as e:
            log.exception("Chat stream failed: %s", e)
            yield {"event": "error", "data": "Something went wrong while answering."}
            yield {"event": "done", "data": ""}

    return EventSourceResponse(event_generator())
