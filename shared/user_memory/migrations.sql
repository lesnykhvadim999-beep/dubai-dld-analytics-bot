-- Phase BM Layer 9 — Long-term User Memory
-- Target DB: RESALE_DATABASE_URL (railway / intelligence)
-- Idempotent. Requires extension `vector` (pgvector).

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ───────────────────────── user_profiles ─────────────────────────
-- One row per (bot_name, user_id). Holds aggregated preferences and
-- a rolling "centroid" embedding of the user's recent interactions.

CREATE TABLE IF NOT EXISTS user_profiles (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL,
  bot_name TEXT NOT NULL,
  embedding vector(384),
  preferences JSONB DEFAULT '{}'::jsonb,
  life_events JSONB DEFAULT '[]'::jsonb,
  last_active TIMESTAMPTZ DEFAULT NOW(),
  total_interactions INT DEFAULT 0,
  language TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (user_id, bot_name)
);

CREATE INDEX IF NOT EXISTS idx_user_profiles_user
  ON user_profiles(user_id);
CREATE INDEX IF NOT EXISTS idx_user_profiles_bot_active
  ON user_profiles(bot_name, last_active DESC);
CREATE INDEX IF NOT EXISTS idx_user_profiles_emb
  ON user_profiles USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);

-- ───────────────────────── user_memory_facts ─────────────────────
-- Atomic facts about the user: budget, family, timeline, area pref,
-- past objections etc. Source-tracked + confidence-scored.

CREATE TABLE IF NOT EXISTS user_memory_facts (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL,
  bot_name TEXT,
  fact_text TEXT NOT NULL,
  fact_type TEXT NOT NULL CHECK (fact_type IN
    ('preference','budget','family','timeline','objection','area','property_type','contact','other')),
  confidence REAL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
  source_message_id BIGINT,         -- references bot_interactions.id (loose; no FK to avoid cascades)
  source_session_id TEXT,
  embedding vector(384),
  metadata JSONB DEFAULT '{}'::jsonb,
  reference_count INT DEFAULT 1,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  last_referenced TIMESTAMPTZ DEFAULT NOW(),
  superseded_by BIGINT
);

CREATE INDEX IF NOT EXISTS idx_user_memory_facts_user
  ON user_memory_facts(user_id, fact_type);
CREATE INDEX IF NOT EXISTS idx_user_memory_facts_active
  ON user_memory_facts(user_id) WHERE superseded_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_user_memory_facts_last_ref
  ON user_memory_facts(last_referenced DESC);
CREATE INDEX IF NOT EXISTS idx_user_memory_facts_emb
  ON user_memory_facts USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ───────────────────────── user_memory_runs ──────────────────────
-- Extraction job audit. Diagnostic only.

CREATE TABLE IF NOT EXISTS user_memory_runs (
  id BIGSERIAL PRIMARY KEY,
  bot_name TEXT,
  user_id BIGINT,
  ts TIMESTAMPTZ DEFAULT NOW(),
  messages_scanned INT DEFAULT 0,
  facts_extracted INT DEFAULT 0,
  facts_merged INT DEFAULT 0,
  llm_provider TEXT,
  llm_ms INT,
  error TEXT
);

CREATE INDEX IF NOT EXISTS idx_user_memory_runs_user_ts
  ON user_memory_runs(user_id, ts DESC);

-- ───────────────────────── consolidation log ─────────────────────
CREATE TABLE IF NOT EXISTS user_memory_consolidation_log (
  id BIGSERIAL PRIMARY KEY,
  run_ts TIMESTAMPTZ DEFAULT NOW(),
  users_processed INT DEFAULT 0,
  duplicates_merged INT DEFAULT 0,
  facts_pruned INT DEFAULT 0,
  duration_ms INT,
  details JSONB
);

-- Top-N active facts helper view
CREATE OR REPLACE VIEW user_memory_active_facts AS
SELECT
  user_id,
  bot_name,
  fact_type,
  fact_text,
  confidence,
  reference_count,
  last_referenced,
  created_at
FROM user_memory_facts
WHERE superseded_by IS NULL
ORDER BY user_id, last_referenced DESC;
