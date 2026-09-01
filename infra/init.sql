-- Yaazhi PostgreSQL Schema — v2 (2026-05-10)
-- Audit fix INF-6: yaazhi_conversations wired up with user_id + language columns.
-- Idempotent: safe to run multiple times.

-- D-04 FIX: Extension creation requires PostgreSQL superuser privileges.
-- POSTGRES_USER (yaazhi) is the owner but may not be superuser in all envs.
-- Switch to postgres superuser for extension DDL only, then switch back.
SET ROLE postgres;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
-- Restore application role for all subsequent DDL
RESET ROLE;

-- ── Semantic memory ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS yaazhi_memories (
    id          UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    content     TEXT        NOT NULL,
    embedding   vector(768),
    metadata    JSONB       DEFAULT '{}',
    source      TEXT        DEFAULT 'manual',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ── Conversations (INF-6 FIX: user_id + language added, now written to) ─────
CREATE TABLE IF NOT EXISTS yaazhi_conversations (
    id          BIGSERIAL   PRIMARY KEY,
    session_id  TEXT        NOT NULL,
    user_id     TEXT        NOT NULL DEFAULT 'default',
    role        TEXT        NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content     TEXT        NOT NULL,
    language    TEXT        DEFAULT 'en',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ── Preferences ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS yaazhi_preferences (
    user_id     TEXT        NOT NULL,
    key         TEXT        NOT NULL,
    value       TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, key)
);

-- ── Ingestion deduplication log ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS yaazhi_ingestion_log (
    id          UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    file_name   TEXT        NOT NULL,
    file_hash   TEXT        UNIQUE NOT NULL,
    chunk_count INT         DEFAULT 0,
    ingested_at TIMESTAMPTZ DEFAULT NOW(),
    source_type TEXT        DEFAULT 'pdf' CHECK (source_type IN ('pdf','docx','pptx','url'))
);

-- ── Indexes ───────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_memories_embedding
    ON yaazhi_memories USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_memories_created_at ON yaazhi_memories (created_at);
CREATE INDEX IF NOT EXISTS idx_memories_source     ON yaazhi_memories (source);
CREATE INDEX IF NOT EXISTS idx_memories_content_trgm
    ON yaazhi_memories USING gin (content gin_trgm_ops);

-- Conversation indexes (INF-6 FIX: session + user + time indexes)
CREATE INDEX IF NOT EXISTS idx_conversations_session
    ON yaazhi_conversations (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversations_user
    ON yaazhi_conversations (user_id, created_at DESC);

-- ── Stats view ────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW yaazhi_stats AS
SELECT source, COUNT(*) AS memory_count,
       MIN(created_at) AS oldest, MAX(created_at) AS newest,
       AVG(length(content)) AS avg_chunk_chars
FROM yaazhi_memories GROUP BY source;

-- ── Permissions ───────────────────────────────────────────────────────────────
GRANT ALL PRIVILEGES ON ALL TABLES    IN SCHEMA public TO yaazhi;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO yaazhi;
GRANT EXECUTE ON ALL FUNCTIONS        IN SCHEMA public TO yaazhi;
