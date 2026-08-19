# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`frontend/` is the Angular 21 UI of **Enterprise AI Workspace**, branded
**Folio** in-product — one of three services in the `enterprise-ai-workspace/`
monorepo:

| Service | Stack | Port | Role |
|---|---|---|---|
| `frontend/` (this) | Angular 21, standalone, zoneless | 4200 | Chat, document pipeline, citations, settings |
| `api/` | ASP.NET Core 8 | 5080 | Persistence + SSE proxy |
| `ai-service/` | FastAPI + Chroma + Redis + Gemini | 8001 | RAG, embeddings, LLM |

The UI talks **only** to the ASP.NET API — `API_BASE` in
[src/app/core/config.ts](src/app/core/config.ts), hardcoded to
`http://localhost:5080/api`. There is no Angular environment file and no proxy
config; change that constant to point elsewhere.

## Commands

```bash
npm install
npm start          # ng serve -> http://localhost:4200
npm run build      # ng build -> dist/frontend
```

Or from the repo root: `docker-compose up frontend` (runs
`ng serve --host 0.0.0.0 --disable-host-check`).

**There is no test runner, no linter, and no `test` script.** `angular.json`
defines only `build` and `serve`. Verification is `npm run build` — which
type-checks under `strict` plus `strictTemplates` — and exercising the UI
against a running API. Do not assume `ng test` works; adding tests means adding
the tooling first.

## Application shape

Everything is standalone components; there is no `NgModule` and no router. One
screen, composed from:

- [app.component.ts](src/app/app.component.ts) — the shell. Sidebar (chats
  tab / documents tab), topbar, and the overlays. Owns conversation
  list/search/rename/delete, global keyboard shortcuts, and the effects that
  prune stale scope and toast ingestion outcomes.
- [chat/chat.component.ts](src/app/chat/chat.component.ts) — transcript,
  composer, streaming, citations, copy/regenerate.
- [documents/documents-panel.component.ts](src/app/documents/documents-panel.component.ts) —
  drag-and-drop upload, six-stage pipeline visualisation, filters, retry.
- [settings/settings.component.ts](src/app/settings/settings.component.ts) —
  theme picker plus measured cache/token/cost stats.
- [onboarding/onboarding.component.ts](src/app/onboarding/onboarding.component.ts) —
  three-step first-run modal, gated on `localStorage['folio.onboarded']`.
- [shared/command-palette.component.ts](src/app/shared/command-palette.component.ts) —
  the Cmd/Ctrl+K palette. Every row is backed by real state (conversations,
  indexed documents, theme, settings); selecting a document genuinely changes
  retrieval scope. Matching is subsequence-based, so `clp` finds
  `company_leave_policy.pdf`.
- [shared/](src/app/shared/) — `LogoComponent`, `ToastsComponent` +
  `ToastService`.

Services worth knowing: `AuthService` (token + headers), `IntegrationsService`
(GitHub connect / repo picker), `StatsService` (measured usage in settings),
`ConversationsService` (list, history, rename, delete).

State lives in `providedIn: 'root'` signal services rather than in components:
`WorkspaceStore`, `DocumentsService`, `ConversationsService`, `ChatService`,
`StatsService`, `ThemeService`, `ToastService`.

## Zoneless: state must be signals

[src/main.ts](src/main.ts) bootstraps without `provideZoneChangeDetection`, and
Angular 21 is zoneless by default — `NgZone` is a `NoopNgZone`, and
`angular.json` sets `"polyfills": []` so `zone.js` is never loaded (it lingers
in `package.json` dependencies but is unused). **Mutating a plain class field
from an async callback will never re-render.** Every component is
`ChangeDetectionStrategy.OnPush` and every piece of shared state is a
`signal()`/`computed()`.

A deliberate refinement in the chat transcript: `ChatMessage.content`,
`.sources`, `.state` and `.meta` are each their own `WritableSignal` rather than
fields inside one immutable array. A streamed token then updates one text node
instead of re-rendering the whole transcript on every delta. Keep that shape
when adding per-message state.

[core/workspace.store.ts](src/app/core/workspace.store.ts) is the cross-component
state: which conversation is open, which sidebar tab is showing, and
`selectedDocumentIds` (empty = search the whole corpus). The sidebar, the
composer's scope bar and the transcript all read it, which is what keeps them in
sync without prop-drilling. `pruneScope()` drops any scoped document that is
gone or no longer `ready`, so a deleted file cannot silently keep constraining
retrieval — `AppComponent` runs it in an effect on every documents change.

## SSE: one parser, two consumers

[core/sse.ts](src/app/core/sse.ts) is the shared, spec-correct reader over
`fetch()`'s `ReadableStream`. `EventSource` is unusable here (POST bodies,
custom headers). Two details are load-bearing:

- **Frames end at a blank line, not at each `data:` line** — reacting per line
  makes the parser depend on how the server happens to chunk.
- **Multiple `data:` lines in one frame join with `\n`.** `sse_starlette`
  splits any value containing a newline across several `data:` lines, so a
  parser that concatenates them flattens every multi-paragraph answer into one
  run-on line.

CRLF and LF are both tolerated so it works against the ASP.NET proxy or FastAPI
directly. `isAbortError` and `describeNetworkError` live here too — `fetch()`
rejects with `TypeError` for DNS/connection/CORS failures, which is why that
case gets the "Is the backend running?" message.

### Chat streaming contract

[chat/chat.service.ts](src/app/chat/chat.service.ts) posts to
`/api/chat/stream` with `{userId, chatId, question, documentIds}` and handles
`sources`, `delta`, `meta`, `error`, `done`; unknown events are ignored rather
than treated as failure. Two guarantees the UI depends on:

1. **`onSettled` fires exactly once, on every path** — clean finish, server
   `error` event, transport failure, abort, or a socket that closes without a
   `done` (reported as "The connection closed before the answer finished"
   rather than hanging). `finish()` in the chat component is the *only* place
   `isStreaming` is cleared, which makes a permanently-disabled composer
   structurally impossible. Preserve the `settled` guard and the
   belt-and-braces `finally`.
2. **`X-Chat-Id` drives conversation continuity.** It is read off the response
   headers and adopted into `WorkspaceStore.activeChatId`; without the API
   exposing it via CORS (`WithExposedHeaders` in `api/Program.cs`) every turn
   starts a new chat.

Note the server sends `sources` **twice**: the retrieved set up front, then a
narrowed set containing only the citations the answer actually used. The second
frame simply overwrites the first via `reply.sources.set(...)` — that is
intended, not a bug to "fix".

An assistant turn that produced no text at all is removed from the transcript;
the error banner carries the explanation instead of an empty bubble.

**The retrieval trace** (`ChatComponent.trace()`) reports what is actually
happening — "Searched 5 documents", "Read 8 relevant passages", "Writing the
answer" — derived from the stream rather than animated. It is shaped by the
`route` event, which the server emits *before* the first delta, so a greeting
shows "Thinking" instead of claiming to search documents it never touched.

**Sources render only once a turn completes.** The server sends `sources`
twice: the retrieved set up front, then a set narrowed to what the answer
actually cited. Rendering the first would show a reader passages the answer
never used.

**A `ResizeObserver` on the thread keeps a pinned reader at the bottom.**
Scrolling on send and on each delta is not enough — the trace gains steps, the
sources block appears at the end, and the action row after that; each adds
height while nothing is scrolling.

### Document progress streaming

[documents/documents.service.ts](src/app/documents/documents.service.ts)
subscribes to `/api/documents/events` with the same parser, and **reconnects
with exponential backoff** (1s → 15s) — a document frozen at "Embedding 62%" is
worse than a brief gap. Frames carry `{changed[], removed[]}` and are merged
into the signal; a malformed frame is skipped rather than tearing down the
stream.

Upload deliberately uses `XMLHttpRequest`, not `fetch`: fetch still has no
upload-progress event, and a bar that jumps 0→100 is exactly the opacity this
UI exists to remove. Uploads run **sequentially** so individual progress bars
stay meaningful. A local placeholder row (`localOnly: true`, id `local-N`) holds
the position until the server returns a real `document_id`, after which SSE
takes over. Deletes are optimistic with snapshot rollback on failure.

The pipeline vocabulary must stay in step with the Python service:
`uploading` (browser-only) → `queued` → `parsing` → `chunking` → `embedding` →
`indexing` → `ready`, plus `failed`. `PIPELINE_STAGES`, `STAGE_INDEX`,
`statusLabel()` and `overallProgress()` all key off it — the browser transfer
owns the first sixth of the combined bar, the server's own `progress` owns the
rest.

## Markdown rendering is security-critical

[chat/markdown.ts](src/app/chat/markdown.ts) is a hand-rolled renderer whose
output is passed to `bypassSecurityTrustHtml`. That is only safe because of its
invariant: **every character of model output is HTML-escaped first, and only
tags this file emits itself are ever added afterwards.** If you extend it, never
interpolate un-escaped input, and keep the link rule restricted to
`http`/`https`/`mailto` so `javascript:` URLs cannot get through.

It is also called on every streamed token, so partial input (an unterminated
code fence, a half-typed `**bold`) must degrade gracefully, not throw. The
component memoises the result per message in a `WeakMap` keyed on raw text.
Code spans are parked behind a NUL sentinel while inline rules run, since NUL
cannot appear in model output and survives `escapeHtml` untouched.

`[Source N]` markers — which the RAG prompt asks the model to emit — are
rewritten into `<span class="citation">`.

Injected HTML cannot carry Angular bindings, so the copy button emitted on code
blocks is plain markup with a `data-copy` hook, handled by delegated click in
`ChatComponent.onProseClick`. Any future control inside rendered Markdown must
follow that pattern.

## Theming

[core/theme.service.ts](src/app/core/theme.service.ts) is tri-state —
`light | dark | system` — not a boolean. "System" has to remain a distinct
choice, or a user who wants to follow their OS can never get back there once
they have clicked the switch. The resolved theme is written to
`<html data-theme>` and `style.colorScheme`, with a transient
`.theme-switching` class so switching does not animate every colour at once.

[src/styles.css](src/styles.css) is the design system: light is the base,
`[data-theme="dark"]` overrides. Every colour, radius, shadow and type step is
a custom property — **nothing downstream hard-codes a hex value**, which is what
keeps the two themes and all the views consistent. Add tokens there rather than
literals in component CSS.

## Conventions

- Standalone components; `templateUrl` + `styleUrls` as separate files (no
  inline templates outside tiny shared pieces like `LogoComponent`).
- Built-in control flow (`@if` / `@for` with `track`), not `*ngIf` / `*ngFor`.
- Signal-based APIs: `inject()`, `viewChild()`, `computed()`, `effect()`,
  `output()`.
- Services expose signals directly (`readonly documents = signal([])`) and
  mutate via `.set()` / `.update()`; components read them in templates.
- API DTO fields keep the server's snake_case where they arrive that way
  (`chunk_count`, `source_path`, `tokens_in`) — the JSON is not re-mapped.
  Angular-side state uses camelCase. Both spellings coexist on purpose.
- Section dividers inside long files use `// ── Name ───`.
- **The frontend never tells the API who it is.** There is no `userId` in any
  request; identity comes from the token the server validates. `AuthService`
  holds the access token and contributes `headers()` to every `fetch`. With
  `Auth:Enabled=false` the API authenticates as the seeded dev user, so
  `headers()` is empty and nothing else changes — wiring Auth0/Supabase means
  calling `setToken()` and nothing more.
- Keyboard shortcuts live in `AppComponent.onGlobalKeydown`: Cmd/Ctrl+K opens
  the command palette, Cmd/Ctrl+Shift+K starts a new chat, `d`/`c` switch
  sidebar tabs, `,` opens settings, `?` lists the shortcuts, Escape closes the
  topmost layer only. The single-key ones are guarded against firing during a
  chord *or while text is selected* — without that, Ctrl+C also matched `c` and
  copying in the Library tab jumped to Chats.
- Comments explain *why* a non-obvious choice was made (zoneless, SSE framing,
  the NUL sentinel, XHR-for-progress). Keep them when editing.
