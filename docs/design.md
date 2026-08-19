# Enterprise AI Workspace — System Design

Branded **Folio** in-product. See the project README for build/run instructions.

This file is the canonical design doc: status, decisions and trade-offs. Update
it as decisions change; treat it as the source of truth that code should match,
not the other way around.

For a full explanation of how everything actually works — every component, why
each technology was chosen, and the path a question takes through the system —
see [how-it-works.md](how-it-works.md).

## Status

| Phase | State | Notes |
|---|---|---|
| 1 — Chat + history | **Done** | SSE streaming, persistence, conversation management |
| 2 — Upload + RAG | **Done** | PDF / Word / Excel / PowerPoint / text / image; staged pipeline with live progress |
| 3 — Retrieval quality | **Done** | Hybrid: vector + literal keyword search fused by RRF, relevance band, MMR, document scoping, filename-aware routing |
| 4 — Agents | **Done** | Dev Agent runs sandboxed git tools **or the user's own GitHub account**; Meeting Agent extracts and persists decisions / action items |
| 5 — MCP server | **Done** | Five tools live over stdio; verified registering and dispatching against the real SDK |
| 6 — Memory | **Done** | Relevance + recency + importance blend, using the corpus embedding model |
| 7 — Eval harness | **Done** | 20 cases scoring retrieval, correctness, groundedness and citation accuracy; persists to `eval_runs`, exits non-zero on regression |

### Code intelligence

The Dev Agent answers from either source, preferring the first:

1. **The user's GitHub account.** They paste a read-only personal access token
   in Settings → Integrations and pick a repository. The agent then calls the
   GitHub REST API *with that token*, so it sees exactly the repositories that
   user can see, private ones included. Authorisation is GitHub's, which means
   this service never has to decide who may read what. API-only, no cloning:
   no unbounded disk, no credential-bearing working copies, no sync problem.
2. **A locally mounted checkout** at `DEV_AGENT_REPO`, for a shared repo an
   operator wants every user to be able to ask about.

### Not yet built

- **GitHub OAuth.** Connection is by pasted personal access token. A proper
  OAuth app would remove the copy-paste step; the storage, encryption and
  agent plumbing would not change.
- **Redis-backed job queue.** Ingestion runs as a FastAPI background task in
  the service process; Redis currently backs the answer cache only. Moving
  ingestion onto a queue is the next step for multi-worker deployment.
- **Frontend test runner.** The Angular app has no test tooling; its behaviour
  is covered by targeted Node scripts (SSE framing, markdown/XSS) rather than
  a runner. The Python suite (`pytest`, 168 tests) is real and wired.
- **OCR.** Images are indexed by filename and dimensions with an explicit
  caveat that their contents were not read.

## Retrieval

Vector search alone produced the worst failure this system had: a fact sitting
*verbatim* in an indexed document, reported as "not mentioned". Three
corrections, each from an observed failure rather than a hunch:

**Hybrid.** Embeddings compress meaning, so a rare token — a policy number, a
codename, an unusual surname — can be present word-for-word and still not
surface. A literal substring pass runs alongside the vector search, and the two
are combined with Reciprocal Rank Fusion. RRF uses only each hit's *rank*
within its own list, which is what makes it valid to merge a distance float
with a yes/no match.

**Keyword selectivity.** A literal match is only evidence when the term is
distinctive. Searching "leave" for a leave-policy question pulled in seven
passages from a novel about leaving a desert. Any term matching more than four
chunks is treated as non-identifying and its hits discarded — inverse document
frequency, in spirit.

**An additive relevance band.** Chunks are kept within a fixed margin of the
best hit for *that query*, not within a multiple of it. A multiple looks
adaptive but scales the wrong way: when the best hit is itself mediocre (1.15),
a 1.9x band admits everything out to 2.18 — effectively the whole corpus, which
is how an unrelated novel ended up cited in a question about taxation.

MMR then diversifies, weighted firmly toward relevance; at a lower weight the
diversity term pushed the single best-matching chunk out of the window. Hits
found only by keyword carry no embedding and so cannot take part in the cosine
comparison — they are re-merged afterwards rather than silently dropped, since
they are precisely what the keyword pass exists to surface.

## Conversation routing

Not every message is a retrieval problem, and treating them alike produced
answers that were technically grounded and completely useless — "who built
you?" was answered by reporting that a novel in the context did not say.

`app/router.py` classifies first, with cheap deterministic rules ahead of the
model so the commonest messages cost nothing extra. Greetings, small talk and
questions about the assistant itself route to a **general-chat path that never
touches retrieval**, using the persona in `app/persona.py`. Repository
vocabulary routes to the Dev Agent, but only when a repository is actually
connected — routing to a capability that cannot answer is worse than not
routing. Transcripts are detected by shape. Everything else is grounded RAG.

The chosen route is emitted as an SSE `route` event *before* the first token,
so the UI can describe what is happening while it happens.

## Failure handling

Provider errors are classified rather than flattened. A Gemini free-tier daily
quota (20 requests/day) is not transient: retrying it three times with
sub-second backoff cannot succeed and hammers an endpoint that already said no.
`quota`, `rate_limit`, `transport` and `fatal` are distinguished; only the
middle two retry, the provider's own `retryDelay` is honoured up to a cap, and
each carries a message a user can act on. "The AI service reported an error"
tells nobody anything.

## Security model

The three tiers trust each other in exactly one direction, and each boundary is
enforced rather than assumed.

**Browser → ASP.NET.** Identity comes from a validated JWT and nothing else.
No endpoint accepts a `userId`; the caller's id is resolved from the token by
`ICurrentUser`, and every query is filtered by it. Ownership is checked before
any document, chat or collection is read, updated or deleted — returning 404
rather than 403, so ids cannot be probed. `Auth:Enabled=false` selects a seeded
development identity and **throws at startup outside Development**, so a
misconfigured deployment fails closed instead of quietly serving every request
as the dev user.

**ASP.NET → FastAPI.** The AI service holds the model keys, the vector store
and every user's documents. It requires a shared `X-Service-Token`, compared in
constant time, so reaching the port is not the same as being authorised. It
performs no user authentication of its own and trusts the `user_id` the API has
already authorised — which is exactly why it must not be directly reachable.

**Uploads.** Validated by leading bytes, not by extension or `Content-Type`,
both of which the client controls. Size is capped at the form, controller and
service layers.

**Dev Agent.** Every tool passes an argument list to `subprocess` (never a
shell string), resolves paths and proves they stay inside the repo root,
excludes `.env` and key material from results, and bounds every call with a
timeout and an output cap. The repository is mounted read-only.

**Third-party tokens.** GitHub tokens are encrypted with AES-256-GCM before
they reach the database, so a dump or a read-only SQL leak does not become a
source-code breach. The API is the sole custodian of the key; it decrypts and
passes the token over the already-authenticated service channel rather than
sharing the key with a second service. The token is never returned to the
browser after it is stored.

**Prompting.** Retrieved text and tool output are untrusted input. No-match
messages never echo the caller's query back into a prompt, and a repository
search is confined to the selected repo by appending the `repo:` qualifier
ourselves rather than trusting one in the query.

## Caching

Answers are cached in Redis under a key derived from everything that shaped
them: user, document scope, normalised question, model, prompt version, **and
the exact chunk ids retrieved**. Including the chunk ids is what makes this
safe rather than merely fast — if re-ingestion changes what retrieval returns,
the key changes with it, so a stale answer cannot be served even if
invalidation were somehow missed. Entries are additionally tagged by document
id and dropped when a document is re-ingested or deleted. Redis being
unavailable degrades to a cache miss, never an error. Bumping `prompt_version`
in `app/config.py` invalidates everything at once when the template changes.

## Service layout

```
frontend/     Angular 21, standalone, zoneless, signal-based state
api/          ASP.NET Core 8 — auth, authorization, persistence, SSE proxy
ai-service/   FastAPI — RAG, agents, embeddings, cache, MCP
  rag/          parsers.py, chunking.py, embedding.py, retrieval.py
  agents/       dev_agent.py (LangGraph-shaped), git_tools.py, meeting_agent.py
  app/          llm_service.py, cache.py, router.py, pipeline.py, security.py
  memory/       store.py — relevance + recency + importance ranking
  mcp_server/   server.py — five tools over stdio
  eval/         run_eval.py + questions.json
  tests/        pytest suite (168 tests)
db/           init.sql + idempotent migrations applied by the API at startup
```

Interfaces are kept narrow so implementations can be swapped: `llm_client.py`
is the only file that imports a model SDK, `embedding.py` the only one that
knows about Chroma, and `parsers.py` the only one that knows about file formats.

## Request path

```
Browser ──JWT──▶ ASP.NET  ──X-Service-Token──▶ FastAPI ──▶ Gemini / Chroma
                    │                              │
                    ├── Postgres (users, chats,     ├── Postgres (documents,
                    │   messages, citations)        │   chunks, memory, usage)
                    └── SSE proxy, frame-preserving └── Redis (answer cache)
```

Intent routing happens in FastAPI, before retrieval: greetings and general
questions skip the vector store entirely, repository questions go to the Dev
Agent, transcripts go to the Meeting Agent, and everything else is grounded RAG
with citations.
