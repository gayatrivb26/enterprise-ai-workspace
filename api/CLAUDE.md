# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`api/` is the ASP.NET Core 8 backend of **Enterprise AI Workspace** (the product
calls itself **Folio** in the UI) — one of three services in the
`enterprise-ai-workspace/` monorepo:

| Service | Stack | Port | Role |
|---|---|---|---|
| `frontend/` | Angular 21, standalone, zoneless | 4200 | Chat + document UI |
| `api/` (this) | ASP.NET Core 8 + EF Core + Npgsql | 5080 | System of record, single browser-facing origin, SSE proxy |
| `ai-service/` | FastAPI + Chroma + Redis + Gemini | 8001 | RAG, chunking, embeddings, LLM, cache, cost ledger |

Supporting containers: Postgres 16 (5432), Chroma 0.5.5 (8000), Redis 7 (6379).

The division of labour is deliberate: **this API owns users/chats/messages and
is the auth entry point; it owns nothing LLM- or vector-specific.** Every model
call, embedding, chunk, retrieval decision, cache entry and pipeline stage lives
in the Python service and is reached only through
[Services/AiServiceClient.cs](Services/AiServiceClient.cs). The browser talks to
this API and nothing else — that is why even pure pass-through resources
(documents, collections, stats) have controllers here.

When adding a feature, decide which side owns it before writing code.

## Commands

Run from `api/`:

```bash
dotnet restore
dotnet build
dotnet run                 # http://localhost:5080, Swagger at /swagger
dotnet watch run           # hot reload
```

`dotnet run` uses `appsettings.Development.json`, which points at `localhost`
for Postgres and the AI service — so Postgres, Chroma, Redis and ai-service must
already be up (`docker-compose up postgres redis chroma ai-service` from the repo
root). Base `appsettings.json` uses Docker service hostnames instead.

Full stack from the repo root:

```bash
cp .env.example .env       # then set GEMINI_API_KEY
docker-compose up --build  # api container builds Dockerfile `target: dev` -> dotnet watch
```

Smoke tests:

```bash
curl http://localhost:5080/health

# With Auth:Enabled=false every call is the seeded dev user, so no token is
# needed locally. Note the absence of userId — identity is never a parameter.
curl "http://localhost:5080/api/documents"
curl -X POST http://localhost:5080/api/documents -F "file=@./page.md"
curl -N "http://localhost:5080/api/documents/events"
curl "http://localhost:5080/api/integrations/github"

curl -N -X POST http://localhost:5080/api/chat/stream   -H "Content-Type: application/json"   -d '{"chatId":null,"question":"What is our leave policy?","documentIds":[]}'
```

With `Auth:Enabled=true`, add `-H "Authorization: Bearer <token>"`.

**There is no test project and no linter configured.** Verification today is
`dotnet build` plus the curl calls above. If you add tests, create a sibling
`api.Tests/` project rather than putting them under this one.

## Endpoint surface

`ChatController` and `IntegrationsController` touch the database; the other
three are typed proxies onto the AI service.

None of these routes take a user id — see "Auth and authorization" below. The
caller is resolved from the token, and every query is filtered by it.

**[Controllers/ChatController.cs](Controllers/ChatController.cs)** — owns Postgres

- `GET /api/chat?search=` → `ChatSummary[]`. Search matches the title
  *or* any message body (`ILIKE`), so a conversation is findable by what was
  said in it, not just its name. Excludes archived, caps at 200.
- `GET /api/chat/{chatId}/history` → full `Message[]` including persisted
  `sources` JSON and per-turn telemetry.
- `PATCH /api/chat/{chatId}` — rename, re-scope (`documentIds`), archive.
- `DELETE /api/chat/{chatId}` — messages cascade at the DB level.
- `POST /api/chat/stream` — the SSE endpoint (see below).

**[Controllers/DocumentsController.cs](Controllers/DocumentsController.cs)**

- `POST /api/documents` (multipart: `file`, optional `collectionId`)
- `GET /api/documents?search=&status=&collectionId=`
- `GET|DELETE /api/documents/{id}`, `POST /api/documents/{id}/reingest`
- `GET /api/documents/events` — long-lived SSE of pipeline progress. Accepts
  `?access_token=` as well as a header, because `EventSource` cannot set one.
- Validated **here as well as** in the AI service, by leading bytes rather than
  extension. The duplication is intentional: rejecting before streaming the
  bytes onward is cheaper and gives the browser a precise message.

**[Controllers/CollectionsController.cs](Controllers/CollectionsController.cs)** —
list/create/delete. Collections group documents so a chat can be scoped to part
of the corpus; storage lives with the documents, in the AI service.

**[Controllers/IntegrationsController.cs](Controllers/IntegrationsController.cs)** —
owns Postgres. `GET|POST|DELETE /api/integrations/github` connects and
disconnects an account; `GET /api/integrations/github/repos` lists what the
token can see; `PUT /api/integrations/github/repo` picks the one the Dev Agent
answers about. The token is verified against GitHub before being stored, and is
never returned to the browser afterwards.

**[Controllers/StatsController.cs](Controllers/StatsController.cs)** —
`GET /api/stats` relays cache + token/cost telemetry for the settings screen.

The proxy controllers share a private `Relay(HttpResponseMessage, ct)`
helper that forwards status code and JSON body verbatim. Keep new pass-through
routes on that pattern rather than deserializing and re-serializing.

## Database: EF Core does not own the schema

Two mechanisms, and both matter:

1. `../db/init.sql` — the full baseline schema, applied by the Postgres
   container **only on a fresh volume** (`/docker-entrypoint-initdb.d/`).
2. `../db/migrations/*.sql` — additive, idempotent SQL applied on **every API
   startup** by [Data/SchemaMigrator.cs](Data/SchemaMigrator.cs), an
   `IHostedService`. That is what keeps an existing volume in step without a
   manual step or a destructive `down -v`.

There are **no EF Core migrations** and none should be added.
[Data/WorkspaceDbContext.cs](Data/WorkspaceDbContext.cs) exists purely to map
PascalCase entities onto snake_case columns — every new property needs an
explicit `HasColumnName`.

Rules for a schema change:

- Add a new numbered file under `../db/migrations/` (ordinal-sorted, so
  `002_*.sql` after `001_workspace_v2.sql`). It **must** be re-runnable: only
  `CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`,
  `DROP CONSTRAINT IF EXISTS` + re-add, guarded `UPDATE`s. Never drop or rewrite
  user data — it runs again on every boot.
- Mirror the change into `../db/init.sql` so fresh volumes match.
- Add the `HasColumnName` mapping if this API reads the column.
- `SchemaMigrator` **logs and continues** when a migration throws, so a failure
  is silent from the outside — check API startup logs after changing SQL. It
  also retries the Postgres connection ten times, because the API routinely wins
  the race against the database container.
- `ResolveMigrationsDirectory` walks up to six levels from the content root
  looking for `db/migrations`, so it resolves both from `bin/Debug` under
  `dotnet run` and from `/src` in the container. Override with
  `Database:MigrationsPath`.

Table ownership: this API maps `users`, `chats`, `messages`, `documents`. The
AI service owns `document_chunks`, `collections`, `memory_entries`, `llm_usage`
and `eval_runs`, and writes `documents` rows during ingestion — which is why
`Document` is mapped here for reads but nothing here inserts one.

A dev user `00000000-0000-0000-0000-000000000001` is seeded by `init.sql` and
hardcoded in the frontend. `userId` is client-supplied on every request and is
not checked against anything.

## The SSE path is the load-bearing part

`POST /api/chat/stream` re-parses the upstream SSE stream frame by frame and
re-emits it, rather than piping bytes, because it has to persist the answer and
its telemetry. Three contracts must hold; breaking any is a user-visible hang or
corruption:

1. **Exactly one terminal event.** A `done` (or `error` then `done`) is emitted
   on every path — success, upstream non-2xx, exception, client cancel. The
   frontend clears its loading state *only* on a terminal outcome, so a missing
   `done` leaves the composer permanently disabled. Upstream `done` events are
   swallowed, and upstream `error` events are recorded into `failure` and
   re-emitted once by the terminal block, so the client can never see two.
2. **SSE framing is preserved.** A delta containing newlines is re-emitted as
   multiple `data:` lines and rejoined with `\n` by the client, per spec.
   `sse_starlette` upstream splits multi-line values this way; a naive
   line-oriented parser flattens every multi-paragraph answer into one line.
3. **The stream is read asynchronously.** Use `ReadLineAsync` and treat `null`
   as end-of-stream. `reader.EndOfStream` blocks a thread-pool thread for the
   whole generation.

Event handling in `DispatchAsync`: `delta` accumulates into the answer *and*
forwards; `sources` and `meta` are captured for persistence *and* forwarded;
`error` is captured only; `done` is dropped; anything unknown is forwarded
unchanged. Keep that shape — the frontend ignores unknown events by design, so
forwarding a new upstream event type costs nothing.

Around the stream:

- **History.** The last `HistoryTurns` (6) messages are read *before* the new
  question is inserted, reversed into chronological order, and sent upstream so
  follow-ups like "and what about contractors?" resolve.
- **Scope.** `chat.DocumentIds` is persisted on the chat and forwarded as
  `document_ids`, which becomes a real `where` filter on the vector store.
  Passing `documentIds` in the request updates the stored scope.
- **Persistence.** The assistant message is written with the final `sources`
  JSON, `tokens_in`/`tokens_out`/`cached` parsed from the `meta` frame, and a
  stopwatch-measured `latency_ms`. Partial answers are persisted after a cancel
  or mid-stream failure using `CancellationToken.None` — the request token is
  already tripped on exactly the paths that need this.

`GET /api/documents/events` is the *other* SSE endpoint and uses the opposite
strategy on purpose: it forwards lines verbatim and flushes on blank lines
(frame boundaries) rather than parsing, because nothing here needs the payload.
Don't "unify" the two.

Other invariants:

- `X-Chat-Id` is returned as a response header and **must** stay in
  `WithExposedHeaders` in [Program.cs](Program.cs) — cross-origin JS cannot read
  it otherwise, and every turn would silently start a new chat.
- `X-Accel-Buffering: no` and `Cache-Control: no-cache, no-transform` keep
  proxies from buffering either stream.
- `Response.ContentType` is assigned, never appended — appending risks a second
  malformed value alongside whatever the framework sets.
- `AiServiceClient` uses `HttpCompletionOption.ResponseHeadersRead` on both
  streaming calls so bytes forward as they arrive.
- The `HttpClient` timeout is `Timeout.InfiniteTimeSpan`. `HttpClient.Timeout`
  covers the whole response, not just the headers, so any finite value kills a
  long generation or a long-lived event stream mid-flight. Cancellation is
  handled by the request `CancellationToken` instead.

## Conventions

- C# 12 primary constructors for DI (`ChatController(WorkspaceDbContext db, ...)`),
  nullable + implicit usings enabled, namespace `EnterpriseAiWorkspace.Api.*`.
- Request/response DTOs are `record`s declared next to their controller
  (`SendMessageRequest`, `UpdateChatRequest`, `ChatSummary`, `CreateCollectionRequest`).
- Default `JsonSerializerDefaults.Web` camelCase on the browser side;
  `AiServiceClient` is the **only** place camelCase (`userId`) is translated to
  the Python service's snake_case (`user_id`). Keep that mapping there.
- Section dividers inside long files use `// ── Name ───`.
- Comments explain *why* a non-obvious choice was made. Keep them when editing.

## Auth and authorization

Implemented, and the reason most of this project's code looks the way it does.

**Identity comes from the token, never the request.** No endpoint accepts a
`userId`. [Auth/CurrentUser.cs](Auth/CurrentUser.cs) resolves the caller from
the validated JWT's `sub` claim and provisions a local `users` row on first
sign-in, keyed on that subject rather than the email (emails get reassigned; a
new joiner would inherit the previous holder's data).

**Ownership is checked before every read and write.** Controllers filter by the
resolved user id and verify ownership of a chat, document or collection before
touching it, returning **404 rather than 403** so ids cannot be probed.

**It fails closed.** `Auth:Enabled=false` selects
[Auth/DevAuthenticationHandler.cs](Auth/DevAuthenticationHandler.cs), which
authenticates everything as the seeded dev user — and [Program.cs](Program.cs)
**throws at startup** if that is set outside Development. A fallback policy
requires an authenticated user on every endpoint unless it opts out, so one
forgotten attribute cannot expose data.

Provider-agnostic by design: Auth0, Supabase, Entra and Keycloak all issue
standard OIDC JWTs, so enabling real auth is `Auth:Authority` + `Auth:Audience`
(plus `Auth:SigningKey` for Supabase, which signs with a project secret rather
than publishing JWKS).

### Secrets this service holds

- `Auth:ServiceToken` — presented to the AI service as `X-Service-Token`. That
  service holds the model keys and every user's vectors; reaching its port must
  not be the same as being allowed to call it.
- `Auth:TokenEncryptionKey` — AES-256-GCM key for stored GitHub tokens
  ([Services/TokenProtector.cs](Services/TokenProtector.cs)). **This service is
  the sole custodian**: it decrypts and forwards the token over the already
  authenticated service channel rather than sharing the key with Python.
  Required outside Development. Rotating it makes stored tokens undecryptable,
  which is handled as "not connected" — users simply reconnect.

Uploads are validated by **leading bytes**, not extension or `Content-Type`
([Services/FileValidator.cs](Services/FileValidator.cs)): both of those are
attacker-controlled, and the parsers downstream are native libraries where
malformed input is exactly where memory-safety bugs live.

## Upstream contract (ai-service)

What this API depends on, so a change on either side is an obvious break:

- `POST /chat/stream` — body `{user_id, question, use_memory, chat_id,
  document_ids[], history[{role,content}]}`; emits `sources` → `delta`* →
  `sources` again (narrowed to the citations the answer actually used) →
  `meta` → `done`, or `error` + `done`.
- `POST|GET /documents`, `GET|DELETE /documents/{id}`,
  `POST /documents/{id}/reingest`, `GET /documents/events`
- `GET|POST /collections`, `DELETE /collections/{id}`
- `GET /stats?user_id=`

The document status values come from that service's pipeline:
`queued → parsing → chunking → embedding → indexing → ready`, or `failed`, with
an integer `progress`. Terminal states are guaranteed on the Python side, so a
document can never sit mid-pipeline forever.

## Doc drift to be aware of

- `.github/copilot-instructions.md` contains only unrelated Azure boilerplate.
- The root [README.md](../README.md) and [docs/design.md](../docs/design.md)
  are current as of the auth, hybrid-retrieval and agent work. `docs/design.md`
  is the canonical design doc and carries an explicit "Not yet built" section —
  keep that honest rather than aspirational.
