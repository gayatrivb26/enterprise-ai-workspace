# How Folio Works

A complete walkthrough of the system: what each piece does, why that technology
was chosen over the alternatives, and how a single question travels through all
of it.

Read this top to bottom and you should be able to explain the whole product.
[design.md](design.md) is the shorter, decision-focused companion;
[README.md](../README.md) is the setup guide.

---

## 1. What the product is

Folio answers questions about **your own material** — company documents and a
connected Git repository — and shows you where each answer came from.

The difference between this and a general chatbot is one word: **grounding**. A
general model answers from what it absorbed during training, which may be
outdated, generic, or confidently wrong about your company. Folio retrieves the
actual passages from your actual files and asks the model to answer *from
those*, citing them. If the passages do not contain the answer, it says so.

That single constraint drives most of the architecture.

---

## 2. The three services, and why it is split this way

```
┌────────────┐      ┌────────────┐      ┌────────────┐
│  frontend  │ ───▶ │    api     │ ───▶ │ ai-service │
│ Angular 21 │      │ ASP.NET 8  │      │  FastAPI   │
│   :4200    │ ◀─── │   :5080    │ ◀─── │   :8001    │
└────────────┘      └────────────┘      └────────────┘
                          │                    │
                    ┌─────┴─────┐    ┌─────────┼─────────┐
                    │ Postgres  │    │ Chroma  │  Redis  │
                    │  :5432    │    │  :8000  │  :6379  │
                    └───────────┘    └─────────┴─────────┘
```

**Why not one service?** Three reasons, in order of importance:

1. **The languages have different strengths.** Every serious ML library —
   sentence-transformers, PyMuPDF, tiktoken, the vector store clients — is
   Python. Meanwhile, ASP.NET Core gives you Entity Framework, first-class JWT
   validation and a strongly typed request pipeline. Forcing either into the
   other's territory means reimplementing something that already exists.

2. **It creates a security boundary.** The AI service holds the model API key,
   the vector store and every user's document text. Keeping it off the public
   network and reachable only through the API means a browser bug cannot reach
   it directly. See [§11](#11-security).

3. **The pieces fail differently.** A slow embedding model should not block a
   user renaming a conversation. Separate processes mean separate failure
   domains.

**The cost** is real: two hops, two deployables, and a serialisation boundary
in the middle. That is the trade being made deliberately, not accidentally.

### Who owns what

| Concern | Owner | Why |
|---|---|---|
| Identity, sessions, authorization | `api` | It is the only service the browser talks to, so it is the only place a token can be validated |
| Users, chats, messages, citations | `api` | Relational, transactional, EF Core handles it well |
| Documents, chunks, memory, usage | `ai-service` | Written by the ingestion pipeline, which lives there |
| Embeddings, retrieval, generation | `ai-service` | Where the Python ML ecosystem is |
| Agents, MCP, evaluation | `ai-service` | All model-adjacent |

The rule: **the API owns nothing LLM- or vector-specific; the AI service owns
nothing about identity.**

---

## 3. The stack, choice by choice

### Angular 21 — the frontend

**What it does:** the entire UI — chat, document library, command palette,
settings, onboarding.

**Why Angular:** the brief called for it. But the version matters enormously,
and this is the single biggest gotcha in the codebase.

**Angular 21 is *zoneless* by default.** Older Angular used a library called
`zone.js` that monkey-patched every async primitive in the browser —
`setTimeout`, `fetch`, event handlers — so the framework could notice "something
async finished, I should re-check the UI". Angular 21 dropped that. Nothing
watches for you.

Instead, state is held in **signals**:

```ts
readonly messages = signal<ChatMessage[]>([]);   // a reactive box
messages.update(list => [...list, newMessage]);  // Angular is told directly
```

A signal is a value that announces its own changes. The template subscribes to
the ones it reads, so updating one re-renders exactly the affected DOM and
nothing else.

**This caused the first bug in this project.** The original chat component
stored messages in a plain array and mutated it from a `fetch` callback. The
network response arrived, the data landed in memory, and the screen never
updated — because nothing told Angular. The fix was not a workaround; it was
using the framework as designed.

One refinement worth understanding: each message's `content` is *its own*
signal rather than a field inside the array. During streaming, a token arrives
every few milliseconds. If content lived in the array, each token would replace
the array and re-render the entire transcript. With per-message signals, a
token updates one text node.

```ts
interface ChatMessage {
  content: WritableSignal<string>;   // one text node re-renders
  sources: WritableSignal<Source[]>;
  state:   WritableSignal<MessageState>;
}
```

### ASP.NET Core 8 — the API

**What it does:** validates tokens, resolves identity, owns conversations,
enforces ownership, proxies streams.

**Why:** JWT bearer validation, EF Core and dependency injection are built in
and well-trodden. Nothing here is exotic — that is the point. The security-
critical layer should use the boring, audited path.

**Entity Framework Core** maps C# classes to Postgres tables, but deliberately
**does not own the schema**. Tables are created by `db/init.sql` and evolved by
idempotent migrations in `db/migrations/`, applied at startup. EF is told the
column names and nothing more. This avoids two services fighting over one
schema — the Python side writes to the same database.

### FastAPI — the AI service

**What it does:** ingestion, retrieval, generation, agents, caching, MCP.

**Why:** it is async-native, has automatic request validation via Pydantic, and
lives where the ML libraries are.

**The async caveat, which caused a real bug.** FastAPI runs on a single event
loop. The Gemini SDK and sentence-transformers are *synchronous* — they block.
Calling them directly from an `async def` freezes the entire service for the
whole generation: no other request progresses, keep-alives stop, disconnects go
unnoticed.

The fix is to push blocking work onto worker threads:

```python
chunks = await run_in_threadpool(retrieve, question, ...)        # blocking call
async for delta in iterate_in_threadpool(generator):             # blocking generator
    yield {"event": "delta", "data": delta}
```

Any time you add a synchronous library call to this service, it needs the same
treatment.

### Postgres 16 — the system of record

**What it does:** everything relational — 10 tables.

| Table | Holds |
|---|---|
| `users` | Local identity, keyed to the auth provider's subject |
| `chats`, `messages` | Conversations, with citations and per-turn telemetry |
| `documents`, `document_chunks` | File metadata, pipeline state, chunk text |
| `collections` | Optional document groupings |
| `integrations` | Encrypted GitHub tokens |
| `memory_entries` | Decisions and action items from meetings |
| `llm_usage` | Token and cost ledger |
| `eval_runs` | Evaluation history |

**Why Postgres:** the data is genuinely relational — a message belongs to a
chat, which belongs to a user; deleting a chat should cascade. It also handles
JSON (`jsonb` for citations) and arrays (`uuid[]` for document scope), so the
few non-relational fields do not need a second database.

### Chroma — the vector database

**What it does:** stores embeddings and answers "which chunks are closest in
meaning to this question?"

**Why a vector database at all?** This is the heart of RAG, so it is worth
being precise.

An **embedding** is a list of numbers representing a piece of text's meaning.
Here, every chunk becomes **384 numbers**. Similar meanings land near each other
in that 384-dimensional space, even with no words in common: *"How much annual
leave do I get?"* sits near *"Employees accrue 25 days of paid holiday"*.

A normal database cannot answer "nearest in meaning" — `LIKE '%leave%'` is
exact-match, and misses everything phrased differently. A vector database
indexes those coordinates and finds nearest neighbours quickly.

**Why Chroma specifically:** it runs as a single container with no tuning, and
the interface is small enough to swap. `rag/embedding.py` is the only file that
imports it, so moving to pgvector, Qdrant or Pinecone is one file.

### Redis 7 — the answer cache

**What it does:** stores completed answers so repeated questions are free and
instant.

**Why:** in a document Q&A tool the same handful of questions get asked
constantly. Each repeat otherwise costs a retrieval, a generation, and several
seconds. Redis is the obvious fit — in-memory, TTL support, sets for tagging.

The *interesting* part is the cache key, covered in [§10](#10-caching).

### Gemini — the language model

**What it does:** turns retrieved passages plus a question into a written
answer. Also used for structured decisions (intent routing, agent planning).

**Why:** a free tier that needs no credit card, which matters for a project
meant to be run by anyone. Configured as `gemini-flash-latest` — the fast, cheap
tier, which is right for RAG because the model is summarising supplied text
rather than reasoning from scratch.

**The abstraction matters more than the vendor.** `app/llm_client.py` is the
*only* file that imports a model SDK. Everything else goes through
`app/llm_service.py`. Swapping to Claude or GPT is one file plus a config
change.

**Known limitation:** the free tier allows roughly **20 requests per day per
model**. Hitting it is not a bug; the system now reports it clearly rather than
showing a generic error.

### sentence-transformers — embeddings

**What it does:** turns text into those 384 numbers. Model: `all-MiniLM-L6-v2`.

**Why a *local* model rather than an embedding API:**

1. **Cost.** Ingesting a 200-page PDF is thousands of embedding calls. Locally
   it is free.
2. **Privacy.** Document text never leaves the machine during indexing.
3. **No rate limits** on the operation you do most.

The trade is quality — a hosted embedding model would retrieve somewhat better.
That is a reasonable trade at this scale, and again isolated to one file.

**Practical note:** the model is ~90 MB and loads on first use, which made the
first upload appear to hang. It is now warmed on a background thread at
startup.

### Docker Compose

Six services with correct startup ordering and health checks, in one command.
Postgres advertises health so the API waits rather than crash-looping.

---

## 4. Ingesting a document

What happens when you drop a file into the library.

```
Browser ──▶ API ──▶ AI service ──▶ background job
   │         │           │              │
   │         │           │              ├─ parse
   │         │           │              ├─ chunk
   │         │           │              ├─ embed
   │         │           │              └─ index
   │         │           │
   └─────────┴───────────┴──── SSE progress ────────┘
```

### Step 0 — upload, with real progress

The browser uses `XMLHttpRequest` rather than `fetch`. This looks
old-fashioned, and it is deliberate: **`fetch` still has no upload-progress
event.** A progress bar that jumps 0 → 100 is exactly the opacity this UI
exists to remove.

Uploads run **sequentially**, not in parallel — concurrent uploads compete for
bandwidth and make every individual progress bar a lie.

### Step 1 — validation, by content

The API checks the file's **leading bytes**, not its extension or
`Content-Type`. Both of those are attacker-controlled. A PDF starts `%PDF`;
Office files are ZIP containers starting `PK\x03\x04`; PNG has an 8-byte
signature. The parsers downstream are native libraries where malformed input is
exactly where memory-safety bugs live, so the check happens before they see it.

### Step 2 — parse into *structured blocks*

`rag/parsers.py` returns blocks carrying metadata, not one flat string:

| Format | Library | Structure preserved |
|---|---|---|
| PDF | PyMuPDF | Page number |
| Word | python-docx | Headings (by style), tables row-wise |
| Excel | openpyxl | Sheet name, row number |
| PowerPoint | python-pptx | Slide number, title |
| Text/Markdown | built-in | Headings |
| Images | Pillow | Dimensions only — no OCR |

**Why structure matters:** it is what lets a citation say *"Budget.xlsx, sheet
Q3"* instead of *"Budget.xlsx"*. Lose it at parse time and it cannot be
recovered.

Spreadsheets get special treatment — each row is paired with its column
headers:

```
Region: EMEA | Revenue: 1.2M | Owner: Priya
```

not `EMEA | 1.2M | Priya`. A retrieved row has to make sense *out of context*,
because that is exactly how the model will see it.

### Step 3 — chunk

Documents are split into ~400-token pieces with 60 tokens of overlap.

**Why chunk at all?** Two reasons. Models have a context limit, so you cannot
paste a 200-page PDF into every question. And precision: retrieving one relevant
paragraph beats retrieving a whole chapter that happens to contain it.

**Why 400 tokens?** Small enough to be precise, large enough to be
self-contained. A 50-token chunk often loses the subject of its own sentence.

**Why overlap?** So a fact spanning a chunk boundary is not cut in half. The
last 60 tokens of one chunk are the first 60 of the next.

**Structure-aware, not fixed-size.** Chunking merges adjacent blocks only when
they share the same context — same sheet, same slide, same section. A chunk
straddling two sheets would cite the wrong one. Blocks that are too large are
split; blocks that are too small are merged, because embedding hundreds of
one-line chunks produces uniformly weak matches.

### Step 4 — embed and index

Chunks are embedded in batches of 64 and written to Chroma with their metadata;
the text and metadata also go to Postgres so citations can be rendered without
querying the vector store.

### Watching it happen

The pipeline reports itself through the document row at each stage:

```
queued → parsing → chunking → embedding → indexing → ready
                                                   ↘ failed
```

The browser subscribes to `/api/documents/events`, a Server-Sent Events stream.
The AI service **polls Postgres** rather than using a pub/sub channel — this
keeps working when ingestion runs in another process or after a restart, and
needs no extra infrastructure. It polls briskly while work is in flight and
lazily when idle.

Both terminal states are guaranteed: every exit path lands on `ready` or
`failed`, so a document can never be stuck mid-pipeline in the UI.

---

## 5. Answering a question

Now the other direction.

```
question
   │
   ├─▶ route ──────────── chat?    ──▶ answer directly, no retrieval
   │                      code?    ──▶ Dev Agent
   │                      meeting? ──▶ Meeting Agent
   │                      docs?    ──▶ ↓
   │
   ├─▶ retrieve (hybrid) ──▶ rank ──▶ filter
   ├─▶ build prompt with numbered sources
   ├─▶ stream generation
   └─▶ filter citations ──▶ persist
```

### Step 1 — routing: decide what the question *is*

Not every message is a retrieval problem. `app/router.py` classifies first:

| Intent | Goes to |
|---|---|
| `chat` | Direct answer, no retrieval |
| `docs` | Grounded RAG with citations |
| `code` | Dev Agent |
| `meeting` | Meeting Agent |

**Cheap rules run before the model.** Greetings, identity questions and obvious
repository vocabulary are settled by pattern matching, because sending "hi" to
an LLM twice — once to classify, once to answer — doubles latency and cost for
the most common message in the product. Only genuinely ambiguous input reaches
the classifier, which is asked for structured JSON so the decision is parsed
rather than interpreted.

**Why this matters more than it sounds.** Before routing existed, "who built
you?" went through document retrieval. The model — behaving *correctly*, given
its instructions — replied that the provided sources did not say who built it,
and helpfully noted they were excerpts from a novel. Technically true. Useless.

Chat-intent messages now take a path that never touches retrieval, using a
persona (`app/persona.py`) that knows what Folio is and is explicitly forbidden
from mentioning sources when there are none.

Routing also respects what exists: repository questions only route to the Dev
Agent when a repository is actually connected. Routing to a capability that
cannot answer is worse than not routing.

### Step 2 — hybrid retrieval

This is the most refined part of the system, and each piece exists because
something failed without it.

**Vector search** embeds the question and finds nearest chunks. Good at meaning,
bad at rare tokens — embeddings compress, so a policy number or an unusual
surname can be present *word for word* and still not surface.

**Keyword search** does literal substring matching. Precise where embeddings are
fuzzy.

**Fusing them** is not obvious: one produces a distance (a float), the other a
yes/no. They are not comparable. **Reciprocal Rank Fusion** solves this by using
only each result's *rank* within its own list:

```
score(chunk) = Σ  1 / (60 + rank_in_that_list)
```

A chunk found by both retrievers accumulates score from both, so agreement
between independent signals wins — without ever comparing a distance to a
boolean.

**Keyword selectivity.** A literal match is only evidence when the term is
*distinctive*. Searching "leave" for a leave-policy question returned seven
passages from a novel about leaving a desert. Any term matching more than four
chunks is now treated as non-identifying and discarded — inverse document
frequency, in spirit.

**The relevance band.** Retrieval always returns *something*; the question is
what to keep. Chunks are kept within a fixed margin (0.35) of the best hit for
that query.

The margin is **additive, not a multiple** — and this distinction caused a real
bug. A multiple looks adaptive but scales the wrong way: with a mediocre best
distance of 1.15, a 1.9× band admits everything out to 2.18, which was
effectively the entire corpus. That is how a novel ended up cited in a question
about taxation.

**MMR** (Maximal Marginal Relevance) then diversifies. Plain top-k often returns
five near-identical chunks from one section, wasting the context window. MMR
trades a little similarity for coverage:

```
score = λ · similarity(chunk, question) − (1−λ) · max similarity(chunk, already_selected)
```

λ is 0.82 — weighted firmly toward relevance. At 0.65 the diversity term was
strong enough to push the single best-matching chunk out of the window.

**Document scoping.** Selecting documents in the UI becomes a real `where`
filter on the vector store, so it constrains retrieval itself rather than
filtering afterwards. Naming a file in the question does the same
automatically — asking to "summarise genpact qn.txt" embeds a sentence about a
*filename*, which appears nowhere in that file's text, so pure similarity
search could never find it.

### Step 3 — the prompt

Retrieved chunks are numbered and labelled:

```
[Source 1] company_leave_policy.pdf, p.2, section: Sick Leave
In cases of unexpected illness, employees must notify...
```

The system instruction tells the model to answer from these, cite inline as
`[Source 1]`, and say so plainly if they do not cover the question — while
explicitly allowing partial answers, because refusing on incomplete coverage
was itself a failure mode.

### Step 4 — stream

Tokens are forwarded to the browser as they are generated. Waiting for a
complete answer feels broken; watching it appear does not.

### Step 5 — filter citations, then persist

After generation, the answer is scanned for `[Source N]` markers and **only
cited sources are shown**. Retrieval always returns its nearest neighbours, so
without this a greeting would arrive decorated with citations it never used.

The answer, its citations and its token counts are written to Postgres, so
reloading a conversation restores the sources too.

---

## 6. Server-Sent Events, and why they are fiddly

Streaming uses SSE — a long-lived HTTP response where the server pushes text
frames.

**Why not WebSockets?** The data flows one way. SSE is plain HTTP: it works
through proxies, reconnects naturally, and needs no separate protocol.

**Why not `EventSource`,** the browser's built-in SSE client? It only does GET
and cannot set headers. Chat is a POST with an `Authorization` header. So the
stream is read manually from `fetch()`'s `ReadableStream`.

Two details in `core/sse.ts` are load-bearing, and both caused bugs:

**Frames end at a blank line, not at each `data:` line.** Reacting per line
makes the parser depend on how the server happens to chunk the response.

**Multiple `data:` lines in one frame join with `\n`.** The Python SSE library
splits any value containing a newline across several `data:` lines. A parser
that concatenates them flattens every multi-paragraph answer into one run-on
line — which is exactly what happened.

**One guarantee holds the UI together:** `onSettled` fires **exactly once, on
every path** — clean finish, server error, transport failure, abort, or a
socket that closes without a `done` event. It is the only place the loading
state is cleared, which makes a permanently-disabled composer structurally
impossible rather than merely unlikely.

---

## 7. The Dev Agent

Answers questions about code: what a repository does, where something is
implemented, who changed it and when.

**Two sources, preferring the first:**

1. **Your GitHub account.** You paste a read-only token; the agent calls the
   GitHub API *with your token*, so it sees exactly what you can see, private
   repositories included. Authorisation is GitHub's — this service never has to
   decide who may read what.
2. **A locally mounted checkout**, for a shared repository.

**API-only, no cloning.** Cloning would mean unbounded disk, credential-bearing
working copies on our filesystem, and a synchronisation problem.

### It is a state machine

```
plan → resolve_repo → overview → search_code → git_history → synthesize
```

- **plan** — structured output decides which tools are needed
- **resolve_repo** — matches repository names in the question against your
  account, because people name a repo in the question far more often than they
  remember to select one
- **overview** — fetches the repository summary and **README**. "What is this
  repo?" cannot be answered by code search: a project's purpose lives in prose,
  not in a grep of its own source
- **search_code / git_history** — the actual lookups
- **synthesize** — writes the answer from tool output only

**On LangGraph:** every node is a pure `(state) → state` function and the edges
are declared as data — exactly the shape `StateGraph` wants. If LangGraph is
installed it is used; if not, a built-in runner walks the same definitions. The
dependency is optional without the fallback being a *different* agent.

### The local tools are sandboxed

They take chat input and run subprocesses, so:

- **No shell.** Argument *lists*, never strings — `; rm -rf /` is an argument.
- **Confined to the repo.** Paths are resolved (collapsing `..` *and* following
  symlinks) and proven inside the root.
- **No user-controlled flags.** Anything option-like goes after `--`.
- **Secrets excluded.** `.env`, `*.pem`, `id_rsa` and friends never reach a
  prompt.
- **Bounded.** Every call has a timeout and an output cap.

These are verified by tests that assert *side effects* — proving an injected
`touch` never created a file — rather than trusting the output text.

---

## 8. The Meeting Agent

Paste a transcript; it extracts decisions and action items as structured data,
deduplicates them against existing memory, and writes them to `memory_entries`.

Detection is by **shape** — long text with several speaker-prefixed lines —
which is far more reliable than asking a model whether something "is" a
transcript.

Deliberately not a LangGraph machine: the pipeline is linear, so a graph would
add a dependency and indirection without buying branching.

---

## 9. Memory

Lets a later conversation answer "what did I commit to last week?"

Ranking blends three signals:

```
score = 0.60 · relevance + 0.25 · recency + 0.15 · importance
```

- **relevance** — semantic similarity, using the same embedding model as the
  corpus, so "what did I promise about billing?" matches "Own the payments
  migration plan" without sharing a word
- **recency** — exponential decay, so nothing falls off a cliff and a highly
  relevant six-week-old decision can still outrank yesterday's noise
- **importance** — by type; a decision outlives an episodic note

Recency alone is a poor proxy: the newest thing you committed to is very often
not what you are asking about.

---

## 10. Caching

Completed answers are cached in Redis for 7 days.

**The key is the whole point.** An answer is only reusable when everything that
produced it is unchanged, so the key hashes:

```
user · document scope · normalised question · model · prompt version · retrieved chunk ids
```

**Including the chunk ids is what makes it safe rather than merely fast.** If
re-ingestion changes what retrieval returns, the key changes too — so a stale
answer cannot be served *even if invalidation were somehow missed*. Correctness
does not depend on remembering to invalidate.

Entries are additionally **tagged by document id** and dropped when a document
is re-ingested or deleted. Bumping `prompt_version` invalidates everything at
once when the template changes.

A cache hit is **replayed in word-aligned pieces** so it still types out — a
user should not be able to tell a cached answer from a fresh one except by how
fast it arrives.

Redis being unavailable degrades to a miss, never an error.

---

## 11. Security

Three tiers, each boundary enforced rather than assumed.

### Browser → API

**Identity comes from the token and nothing else.** No endpoint accepts a
`userId`. This is not paranoia — the original code took `?userId=` from the
query string, meaning anyone could read anyone's conversations by editing a
URL. A user id supplied by the client is an access-control decision made by
whoever controls the client.

Ownership is checked before every read and write, returning **404 rather than
403** so ids cannot be probed for existence.

**It fails closed.** The development identity is refused outside Development —
the API throws at startup rather than silently serving every request as one
user. A fallback authorization policy requires authentication everywhere unless
a route opts out, so one forgotten attribute cannot expose data.

### API → AI service

The AI service holds the model key, the vector store and every user's document
text. It requires a shared `X-Service-Token`, compared in **constant time** (a
naive `==` leaks a secret one byte at a time to anyone who can measure latency).
Being reachable on the network must not be the same as being authorised.

### Stored third-party tokens

GitHub tokens are encrypted with **AES-256-GCM** before touching the database —
a dump or read-only SQL leak must not become a source-code breach. GCM rather
than plain CBC because it *authenticates* the ciphertext: tampering is detected
on decrypt instead of silently yielding a corrupted token.

The API is the **sole custodian** of the key; it decrypts and forwards the token
over the already-authenticated service channel rather than sharing the key with
a second service.

### Rendering model output

Markdown is rendered by a hand-written renderer whose output is trusted HTML.
That is only safe because of one invariant: **every character is HTML-escaped
first, and only tags the renderer emits itself are added afterwards.** Links are
restricted to `http`/`https`/`mailto` so `javascript:` URLs cannot get through.
26 tests cover this, including injection attempts.

### Prompt hygiene

Retrieved text and tool output are untrusted input. "No results" messages never
echo the caller's query back into a prompt, and repository searches are confined
to the selected repo by appending the `repo:` qualifier ourselves rather than
trusting one in the query.

---

## 12. MCP

The Model Context Protocol lets external clients — Claude Desktop, Claude Code —
use these capabilities as tools. `mcp_server/server.py` exposes five over stdio:
`search_docs`, `list_documents`, `explain_code`, `summarize_meeting`,
`recall_memory`.

**On identity:** stdio MCP has no login. The client launches the process and is
trusted by virtue of running as that OS user, so the workspace is fixed at
startup by an environment variable. This must never be exposed as a shared
network service — there would be no way to tell callers apart.

---

## 13. Evaluation

"Did my change make retrieval better or worse?" is unanswerable by intuition.
`eval/run_eval.py` scores 20 questions on four metrics:

| Metric | Question it answers |
|---|---|
| **retrieval** | Did the expected document appear in top-k? |
| **correctness** | Do the expected facts appear in the answer? |
| **groundedness** | Did it cite real sources — and only real ones? |
| **citation** | Do the cited sources point at the right document? |

**Groundedness is the interesting one.** A citation pointing past the end of the
retrieved set is a *fabrication*, and scores zero outright — the clearest
hallucination signal available. The set includes deliberately unanswerable
questions, where citing nothing is the correct behaviour: a grounded assistant
must be measured on what it refuses, not only on what it answers.

Runs persist to `eval_runs` and exit non-zero on regression, so CI can gate on
it.

---

## 14. Testing

168 Python tests, no external services required:

| Area | Covers |
|---|---|
| SSE parsing | Framing, multi-line data, every chunk-boundary split, never hanging |
| Markdown | XSS, injection, partial input during streaming |
| Git tools | Path traversal, shell injection (by side effect), secret exclusion |
| Parsers | Every format, structure preservation, malformed input |
| Retrieval | Salient terms, rank fusion, the relevance band |
| Routing | Identity and small talk never reaching retrieval |
| Cache keys | That changed retrieval changes the key |
| Memory | Relevance beating recency |
| Eval metrics | That a fabricated citation scores zero |
| Error handling | Daily quota not retried; messages are actionable |

The frontend has **no test runner** — an honest gap. Its behaviour is covered by
targeted Node scripts rather than a wired-up framework.

---

## 15. The UI

**Folio** — the name is a bound collection of documents.

- **Command palette (⌘K)** — every row backed by real state; subsequence
  matching, so `clp` finds `company_leave_policy.pdf`
- **Retrieval trace** — "Searched 5 documents → Read 8 passages → Writing",
  derived from the stream rather than animated. A spinner that says nothing for
  four seconds reads as a hang
- **Citations** — click any to read the passage that was actually used
- **Live pipeline** — six stages per document, updating over SSE
- **Themes** — light/dark/system, persisted; "system" stays a distinct choice,
  or a user who wants to follow their OS can never get back
- **Onboarding, toasts, keyboard shortcuts, responsive layout**

---

## 16. What is deliberately not built

Honesty about limits is part of the design.

| Not built | Why |
|---|---|
| GitHub OAuth | A pasted token works; OAuth is polish, and the storage and agent plumbing would not change |
| Redis job queue | Ingestion is an in-process background task. Fine for one worker; a queue is the next step for several |
| OCR | Images are indexed by filename and dimensions, with an explicit note that contents were not read |
| Frontend test runner | A real gap, honestly labelled |
| Re-ranking by default | Implemented behind a flag; it costs a model call per query and hybrid retrieval closed most of the gap |

---

## Glossary

**Embedding** — a list of numbers representing text's meaning; similar meanings
land near each other. 384 numbers per chunk here.

**Vector database** — a store that finds nearest neighbours in embedding space.

**Chunk** — a passage a document is split into for retrieval. ~400 tokens here.

**RAG** — Retrieval-Augmented Generation: retrieve relevant passages, then ask
the model to answer *from those*.

**Grounding** — constraining an answer to supplied sources rather than the
model's own recall.

**Token** — roughly ¾ of a word; the unit models read, generate and bill in.

**MMR** — Maximal Marginal Relevance: picks results that are relevant *and*
different from each other.

**RRF** — Reciprocal Rank Fusion: merges ranked lists using positions rather
than scores, so incomparable signals can be combined.

**SSE** — Server-Sent Events: a long-lived HTTP response the server pushes
frames down.

**Signal** — Angular's reactive value; the thing that tells a zoneless app to
re-render.

**MCP** — Model Context Protocol: a standard for exposing tools to AI clients.

---

Built by **Gayatri Bhosale**.
