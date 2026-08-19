-- ============================================================================
-- 001_workspace_v2 — document pipeline, collections, citations, LLM usage.
--
-- Fully idempotent and additive: the API executes this on every startup
-- (see api/Data/SchemaMigrator.cs), so it must be safe to run repeatedly
-- against both a fresh database and one that already holds real data.
-- Nothing here drops or rewrites user content.
-- ============================================================================

-- ── Collections ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS collections (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    color       TEXT NOT NULL DEFAULT 'indigo',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_collections_user ON collections(user_id);

-- ── Documents: ingestion pipeline tracking ─────────────────────────────────
ALTER TABLE documents ADD COLUMN IF NOT EXISTS collection_id UUID
    REFERENCES collections(id) ON DELETE SET NULL;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS progress    INT NOT NULL DEFAULT 0;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS error       TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS size_bytes  BIGINT NOT NULL DEFAULT 0;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS page_count  INT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS chunk_count INT NOT NULL DEFAULT 0;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS token_count INT NOT NULL DEFAULT 0;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS updated_at  TIMESTAMPTZ NOT NULL DEFAULT now();

-- The original CHECK only allowed pending/embedded/failed. The pipeline now
-- reports a real stage, so widen it (keeping the legacy values valid so any
-- existing rows stay legal) and then migrate old rows onto the new vocabulary.
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_status_check;
ALTER TABLE documents ADD CONSTRAINT documents_status_check CHECK (status IN (
    'queued', 'parsing', 'chunking', 'embedding', 'indexing', 'ready', 'failed',
    'pending', 'embedded'   -- legacy, retained so old rows remain valid
));
UPDATE documents SET status = 'ready',  progress = 100 WHERE status = 'embedded';
UPDATE documents SET status = 'queued', progress = 0   WHERE status = 'pending';

-- Plain text is cheap to support and covers most internal notes/exports.
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_type_check;
ALTER TABLE documents ADD CONSTRAINT documents_type_check
    CHECK (type IN ('pdf', 'markdown', 'text'));

CREATE INDEX IF NOT EXISTS idx_documents_user   ON documents(user_id);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);

-- Documents ingested before these counters existed would otherwise report
-- "0 chunks" in the UI despite being fully indexed. Backfill from the chunks
-- that actually exist; the chunk_count = 0 guard keeps this a no-op afterwards.
UPDATE documents d
   SET chunk_count = c.n
  FROM (SELECT document_id, COUNT(*) AS n FROM document_chunks GROUP BY document_id) c
 WHERE c.document_id = d.id AND d.chunk_count = 0;

-- ── Chats: conversation management ─────────────────────────────────────────
ALTER TABLE chats ADD COLUMN IF NOT EXISTS updated_at   TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE chats ADD COLUMN IF NOT EXISTS archived     BOOLEAN NOT NULL DEFAULT false;
-- Documents this conversation is scoped to. Empty array = search everything.
ALTER TABLE chats ADD COLUMN IF NOT EXISTS document_ids UUID[] NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS idx_chats_user_updated ON chats(user_id, updated_at DESC);

-- ── Messages: persisted citations + per-turn telemetry ─────────────────────
-- Citations previously lived only in the SSE stream, so reloading a
-- conversation lost every source. They belong with the message.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS sources    JSONB NOT NULL DEFAULT '[]';
ALTER TABLE messages ADD COLUMN IF NOT EXISTS tokens_in  INT NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS tokens_out INT NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS cached     BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS latency_ms INT NOT NULL DEFAULT 0;

-- ── LLM usage / cost ledger ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llm_usage (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID REFERENCES users(id) ON DELETE SET NULL,
    chat_id         UUID,
    operation       TEXT NOT NULL,               -- 'chat' | 'rerank' | 'title' | ...
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL DEFAULT 'v1',
    tokens_in       INT NOT NULL DEFAULT 0,
    tokens_out      INT NOT NULL DEFAULT 0,
    cost_usd        NUMERIC(12, 6) NOT NULL DEFAULT 0,
    cache_hit       BOOLEAN NOT NULL DEFAULT false,
    latency_ms      INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_user ON llm_usage(user_id, created_at DESC);

-- ── Chunk lookup used to render citation previews ──────────────────────────
CREATE INDEX IF NOT EXISTS idx_chunks_vector_id ON document_chunks(vector_id);
