-- Enterprise AI Workspace — initial schema (Phase 1-4 tables included now
-- so later phases don't require destructive migrations)
-- Applied automatically by the postgres container on first boot
-- (mounted into /docker-entrypoint-initdb.d/)

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email       TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    auth_sub    TEXT UNIQUE,          -- Auth0/Supabase subject id
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chats (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title       TEXT NOT NULL DEFAULT 'New chat',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id     UUID NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id);

CREATE TABLE IF NOT EXISTS documents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('pdf','markdown')),
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','embedded','failed')),
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS document_chunks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_text      TEXT NOT NULL,
    chunk_metadata  JSONB NOT NULL DEFAULT '{}',  -- page, heading, source_path
    vector_id       TEXT,                          -- id in Chroma
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON document_chunks(document_id);

CREATE TABLE IF NOT EXISTS memory_entries (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type         TEXT NOT NULL CHECK (type IN ('preference','fact','episodic')),
    content      TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    question          TEXT NOT NULL,
    expected_answer   TEXT,
    retrieved_chunks  JSONB,
    actual_answer     TEXT,
    retrieval_score   NUMERIC,
    answer_score      NUMERIC
);

-- Seed a dev user so Phase 1 works without wiring real auth yet
INSERT INTO users (id, email, name)
VALUES ('00000000-0000-0000-0000-000000000001', 'dev@local.test', 'Dev User')
ON CONFLICT DO NOTHING;
