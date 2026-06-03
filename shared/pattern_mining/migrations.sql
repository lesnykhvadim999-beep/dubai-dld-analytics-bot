-- Phase BL Level 3 — Bot Pattern Mining
-- Target: resale DB (RESALE_DATABASE_URL / DATABASE_URL)
-- Idempotent: safe to run multiple times.

CREATE TABLE IF NOT EXISTS bot_pattern_discoveries (
  id BIGSERIAL PRIMARY KEY,
  discovered_at TIMESTAMPTZ DEFAULT NOW(),
  bot_name TEXT,
  issue_type TEXT,                       -- missing_building_alias|missing_area_alias|slow_query|bad_response|abandoned_session
  affected_users INT,
  severity TEXT,                         -- low|medium|high|critical
  evidence_json JSONB,                   -- list of sample user_message
  suggested_fix_json JSONB,              -- {kind, category, key, value, ...}
  confidence NUMERIC,
  status TEXT DEFAULT 'pending',         -- pending|approved|rejected|applied|auto_rejected
  admin_message_id BIGINT,
  applied_at TIMESTAMPTZ,
  applied_result JSONB,
  fingerprint CHAR(40)                    -- sha1 of cluster signature (dedup)
);

CREATE INDEX IF NOT EXISTS idx_bpd_status_ts
  ON bot_pattern_discoveries(status, discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_bpd_bot_ts
  ON bot_pattern_discoveries(bot_name, discovered_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_bpd_fingerprint_pending
  ON bot_pattern_discoveries(fingerprint)
  WHERE status IN ('pending', 'approved');

-- Apply-side tables (created lazily by bot_pattern_applier; defined here too for visibility).
CREATE TABLE IF NOT EXISTS bot_aliases (
  id BIGSERIAL PRIMARY KEY,
  category TEXT NOT NULL,
  alias_key TEXT NOT NULL,
  canonical_values JSONB NOT NULL,
  source TEXT DEFAULT 'pattern_mining',
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (category, alias_key)
);

CREATE TABLE IF NOT EXISTS bot_cache_config (
  id BIGSERIAL PRIMARY KEY,
  query_pattern TEXT NOT NULL,
  ttl_minutes INT DEFAULT 60,
  source TEXT DEFAULT 'pattern_mining',
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (query_pattern)
);

CREATE TABLE IF NOT EXISTS bot_code_change_proposals (
  id BIGSERIAL PRIMARY KEY,
  discovery_id BIGINT,
  issue TEXT,
  suggested_fix JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  addressed BOOLEAN DEFAULT false
);
