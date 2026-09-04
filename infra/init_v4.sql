-- Yaazhi V4 — PostgreSQL Schema Extension (init_v4.sql)
-- Run after init.sql (infra/init.sql) which creates extensions and base tables.
-- Idempotent: safe to run multiple times.
-- Implements the V4 SQL schema from the operational framework document.

-- ── Tiṇai Enum ───────────────────────────────────────────────────────────────
-- Creates only if the type does not already exist (PostgreSQL 14+ compatible).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'tinai_category') THEN
        CREATE TYPE tinai_category AS ENUM (
            'arch_linux_dev',
            'bms_ml_project',
            'campus_booking_app',
            'general_technical',
            'personal_identity'
        );
    END IF;
END$$;

-- ── Episodic Events (Puram Domain — Layer 2) ─────────────────────────────────
-- Distinct from yaazhi_conversations (turn-level).
-- Stores structured execution telemetry with Tiṇai + Kālam metadata.
-- Embedding column populated asynchronously by the reflection pipeline.
CREATE TABLE IF NOT EXISTS episodic_events (
    event_id         UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    timestamp        TIMESTAMPTZ DEFAULT NOW(),
    tinai            tinai_category                       DEFAULT 'general_technical',
    kalam_epoch      VARCHAR(50)                          DEFAULT 'University_Builder_Epoch',
    orchestrator_id  VARCHAR(50),
    event_type       VARCHAR(50),
    summary          TEXT,
    context_payload  JSONB                                DEFAULT '{}',
    embedding        vector(1536)  -- null until reflection pipeline runs
);

-- ── Decisions Ledger (Puram Domain — Layer 6) ────────────────────────────────
-- Immutable log of architectural / technical decisions.
-- Queried in Step 2 of context assembly to rule out rejected approaches.
CREATE TABLE IF NOT EXISTS decisions_ledger (
    decision_id           UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    timestamp             TIMESTAMPTZ DEFAULT NOW(),
    topic                 VARCHAR(150),
    chosen_path           TEXT,
    reasoning             TEXT,
    rejected_alternatives JSONB       DEFAULT '[]',
    goal_id               UUID,
    tinai                 tinai_category DEFAULT 'general_technical'
);

-- ── Akam User State (Akam Domain — Layers 4, 7, 11, 12) ─────────────────────
-- Single-row-per-user live snapshot of private cognitive state.
-- Updated by the reflection pipeline's affective update step.
CREATE TABLE IF NOT EXISTS akam_user_state (
    state_id            UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id             TEXT        NOT NULL UNIQUE DEFAULT 'default',
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    burnout_score       FLOAT       DEFAULT 0.0 CHECK (burnout_score BETWEEN 0.0 AND 1.0),
    preferred_tone      VARCHAR(100) DEFAULT 'Dry, Concise, Code-First',
    active_constraints  JSONB       DEFAULT '{}',  -- hardware, local LLMs, env prefs
    identity_summary    TEXT        DEFAULT '',     -- Layer 4 personality snapshot
    meta_epochs         JSONB       DEFAULT '[]',  -- Layer 11 learning epoch log
    objectives          JSONB       DEFAULT '[]'   -- Layer 12 long-horizon objectives
);

-- ── Indexes ───────────────────────────────────────────────────────────────────

-- Episodic events: fast Tiṇai + time queries for reflection pipeline batches
CREATE INDEX IF NOT EXISTS idx_events_tinai_time
    ON episodic_events (tinai, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_events_type
    ON episodic_events (event_type, timestamp DESC);

-- Decisions ledger: fast topic + Tiṇai lookup during context assembly Step 2
CREATE INDEX IF NOT EXISTS idx_decisions_topic
    ON decisions_ledger (topic, tinai);

CREATE INDEX IF NOT EXISTS idx_decisions_goal
    ON decisions_ledger (goal_id);

-- Episodic events embedding index (HNSW, built after embeddings are populated)
CREATE INDEX IF NOT EXISTS idx_events_embedding
    ON episodic_events USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- ── Permissions ───────────────────────────────────────────────────────────────
GRANT ALL PRIVILEGES ON TABLE episodic_events    TO yaazhi;
GRANT ALL PRIVILEGES ON TABLE decisions_ledger   TO yaazhi;
GRANT ALL PRIVILEGES ON TABLE akam_user_state    TO yaazhi;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO yaazhi;
