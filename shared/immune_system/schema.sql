-- immune_system schema (PHASE BN N2)
-- Tables: incidents (raw exceptions), proposals (LLM patch+test),
-- applied_immunities (registry of merged guards verified weekly).

CREATE TABLE IF NOT EXISTS immune_incidents (
  id BIGSERIAL PRIMARY KEY,
  signature TEXT UNIQUE NOT NULL,
  bot_name TEXT,
  exception_class TEXT,
  stack_trace TEXT,
  file_path TEXT,
  line_no INT,
  user_id BIGINT,
  env JSONB,
  first_seen TIMESTAMPTZ DEFAULT NOW(),
  last_seen TIMESTAMPTZ DEFAULT NOW(),
  occurrence_count INT DEFAULT 1,
  diagnosis JSONB,
  status TEXT DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_immune_status
  ON immune_incidents(status, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_immune_bot
  ON immune_incidents(bot_name, last_seen DESC);

CREATE TABLE IF NOT EXISTS immunization_proposals (
  id BIGSERIAL PRIMARY KEY,
  incident_id BIGINT REFERENCES immune_incidents(id) ON DELETE CASCADE,
  patch_diff TEXT,
  regression_test_code TEXT,
  rationale TEXT,
  llm_provider TEXT,
  sandbox_passed BOOLEAN DEFAULT FALSE,
  status TEXT DEFAULT 'pending',
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_immune_proposal_status
  ON immunization_proposals(status, created_at DESC);

CREATE TABLE IF NOT EXISTS applied_immunities (
  id BIGSERIAL PRIMARY KEY,
  incident_signature TEXT NOT NULL,
  proposal_id BIGINT,
  patch_commit_sha TEXT,
  regression_test_path TEXT,
  applied_at TIMESTAMPTZ DEFAULT NOW(),
  last_verified TIMESTAMPTZ,
  verification_status TEXT
);
CREATE INDEX IF NOT EXISTS idx_immune_applied_sig
  ON applied_immunities(incident_signature);
