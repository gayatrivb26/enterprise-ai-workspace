-- ============================================================================
-- 002_integrations — per-user connections to external providers (GitHub).
--
-- Idempotent and additive, like 001: the API runs every migration on each
-- startup, so this must be safe to re-apply.
-- ============================================================================

CREATE TABLE IF NOT EXISTS integrations (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider       TEXT NOT NULL,

    -- AES-256-GCM ciphertext, base64. Never the raw token: a database dump,
    -- a stray backup or a read-only SQL leak would otherwise hand out live
    -- credentials to someone's source code.
    access_token   TEXT NOT NULL,

    account_login  TEXT,
    account_name   TEXT,
    avatar_url     TEXT,
    scopes         TEXT,

    -- The repository the user picked for the Dev Agent to answer against.
    selected_repo  TEXT,

    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One connection per provider per user; reconnecting replaces the token.
CREATE UNIQUE INDEX IF NOT EXISTS idx_integrations_user_provider
    ON integrations(user_id, provider);
