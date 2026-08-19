# Folio — Enterprise AI Workspace

An internal knowledge assistant: upload your company's documents, connect a
GitHub account, and ask questions that get answered **with a citation behind
every claim**. Built to exercise RAG, agents, memory, MCP and LLM evaluation in
a real product rather than a demo.

**New to the project? Start with [docs/how-it-works.md](docs/how-it-works.md)** —
a full walkthrough of every component, why each technology was chosen, and how a
question travels through the whole system.

Shorter, decision-focused notes live in [docs/design.md](docs/design.md).

## What it does

- **Ask your documents.** PDF, Word, Excel, PowerPoint, plain text and images.
  Answers cite the exact passages used, and each citation opens a preview.
- **Ask your code.** Connect a GitHub account and ask what a repo does, where
  something is implemented, or who changed it and when — private repos
  included, because it acts with your own token.
- **Summarise meetings.** Paste a transcript; decisions and action items are
  extracted and remembered for later conversations.
- **Talk normally.** Greetings, general knowledge and questions about the
  assistant itself never touch retrieval, so they get real answers instead of
  "the provided sources do not contain…".
- **Use it from anywhere.** The same capabilities are exposed as MCP tools for
  Claude Desktop or any other MCP client.

## Architecture

| Service | Stack | Port | Owns |
|---|---|---|---|
| `frontend/` | Angular 21, standalone, zoneless, signals | 4200 | The whole UI |
| `api/` | ASP.NET Core 8, EF Core, Npgsql | 5080 | Auth, authorization, users/chats/messages, SSE proxy |
| `ai-service/` | FastAPI, Chroma, Redis, Gemini | 8001 | RAG, agents, embeddings, cache, cost ledger, MCP |

Plus Postgres 16, Chroma 0.5.5 and Redis 7.

The split is deliberate: **the API owns identity and is the only origin the
browser talks to; the AI service owns everything model- or vector-shaped.**

```
Browser ──JWT──▶ ASP.NET ──X-Service-Token──▶ FastAPI ──▶ Gemini / Chroma
                    │                            │
                    ├── Postgres (users, chats,   ├── Postgres (documents,
                    │   messages, citations)      │   chunks, memory, usage)
                    └── SSE proxy                 └── Redis (answer cache)
```

## Running it

```bash
cp .env.example .env      # then fill in the three values below
docker compose up --build
```

| Variable | Why |
|---|---|
| `GEMINI_API_KEY` | Free key from [aistudio.google.com](https://aistudio.google.com), no card needed |
| `SERVICE_TOKEN` | Shared secret the API presents to the AI service. Without it, anything that can reach port 8001 can impersonate any user |
| `TOKEN_ENCRYPTION_KEY` | Encrypts stored GitHub tokens. Required outside Development — the API refuses to start without it |
| `DEV_AGENT_REPO_HOST` | *(optional)* a checkout the Dev Agent can answer about for everyone, instead of per-user GitHub |

Generate the two secrets:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

- UI: <http://localhost:4200>
- API: <http://localhost:5080> (Swagger at `/swagger` in Development)
- AI service: <http://localhost:8001/health>

A dev user is seeded so you can start immediately.

### Running services individually

The API and AI service can run outside Docker against the containerised
Postgres/Chroma/Redis:

```bash
docker compose up postgres redis chroma      # dependencies only

cd api        && dotnet watch run            # :5080
cd ai-service && uvicorn app.main:app --reload --port 8001
cd frontend   && npm install && npm start    # :4200
```

For a local API, put the encryption key in .NET user secrets rather than a file:

```bash
cd api
dotnet user-secrets init
dotnet user-secrets set "Auth:TokenEncryptionKey" "<generated value>"
```

## Authentication

`Auth:Enabled=false` (the default) authenticates every request as the seeded
dev user, so the stack runs without provisioning an identity provider. Outside
Development that configuration **throws at startup** — a misconfigured
deployment fails closed rather than silently serving everyone as one user.

To enable real auth, set the provider's authority and audience; Auth0, Supabase,
Entra and Keycloak all issue standard OIDC JWTs and need nothing provider-
specific:

```
Auth__Enabled=true
Auth__Authority=https://your-tenant.eu.auth0.com/
Auth__Audience=https://folio.api
Auth__SigningKey=…        # Supabase only: it signs with a project secret
```

No endpoint accepts a user id. Identity comes from the token, and ownership is
checked before any document, chat or collection is read or written.

## Connecting GitHub

**Settings → Integrations → Connect**, then paste a read-only personal access
token (`repo` for private repositories, `public_repo` otherwise) and pick a
repository. The token is verified, encrypted with AES-256-GCM and never
returned to the browser again.

Questions then route to the Dev Agent automatically. Naming a repository in the
question targets it, even if a different one is selected.

## Testing

```bash
cd ai-service
pytest                      # 168 tests, no external services required
```

Covers SSE framing, Markdown/XSS rendering, git-tool sandboxing (path
traversal, shell injection, secret exclusion), document parsing, hybrid
retrieval, intent routing, cache-key derivation, memory ranking, eval metrics
and provider-error classification.

Retrieval quality has its own harness:

```bash
python -m eval.run_eval     # scores retrieval, correctness, groundedness, citations
```

It exits non-zero when a case falls below threshold, so it can gate CI.

## MCP

```bash
pip install "mcp[cli]"
python -m mcp_server.server
```

Exposes `search_docs`, `list_documents`, `explain_code`, `summarize_meeting`
and `recall_memory` over stdio. The file header carries a ready-made
`claude_desktop_config.json` block.

stdio MCP has no login, so the workspace is fixed at startup by
`FOLIO_USER_ID`. Never expose it as a shared network service.

## Layout

```
frontend/       Angular 21 — chat, library, palette, settings, onboarding
api/            ASP.NET Core 8
  Auth/           JWT validation, dev identity, current-user resolution
  Controllers/    chat, documents, collections, integrations, stats
  Services/       AiServiceClient, FileValidator, TokenProtector
ai-service/     FastAPI
  rag/            parsers, chunking, embedding, retrieval
  agents/         dev_agent (LangGraph-shaped), git_tools, github_client, meeting_agent
  app/            llm_service, cache, router, pipeline, persona, security
  memory/         relevance + recency + importance ranking
  mcp_server/     five tools over stdio
  eval/           harness + 20-case question set
  tests/          pytest suite
db/             init.sql + idempotent migrations, applied by the API at boot
```

## Known limitations

- **Gemini's free tier allows ~20 requests per day per model.** Exhausting it
  surfaces a clear message; it resets every 24 hours, or add billing.
- **GitHub connects by pasted token, not OAuth.** No "Sign in with GitHub" yet.
- **Ingestion runs in-process**, as a FastAPI background task. Redis backs the
  answer cache only; a job queue is the next step for multi-worker deployment.
- **No OCR.** Images are indexed by filename and dimensions, with an explicit
  note that their contents were not read.
- **No frontend test runner.** The Angular app has no test tooling; its
  behaviour is covered by targeted Node scripts. The Python suite is real.

Built by **Gayatri Bhosale**.
