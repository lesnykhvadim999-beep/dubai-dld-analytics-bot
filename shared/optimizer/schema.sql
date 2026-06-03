-- PHASE BN N5: continuous performance optimizer schema.
-- Apply with:
--   psql "$LIVE_DATABASE_URL" -f C:/Projects/shared/optimizer/schema.sql

CREATE TABLE IF NOT EXISTS slow_query_log (
  id BIGSERIAL PRIMARY KEY,
  query_normalized TEXT,
  mean_time_ms REAL,
  calls BIGINT,
  total_time_ms REAL,
  captured_at TIMESTAMPTZ DEFAULT NOW(),
  explain_plan TEXT,
  suggested_index TEXT
);
CREATE INDEX IF NOT EXISTS idx_slow_query_time ON slow_query_log(captured_at DESC);

CREATE TABLE IF NOT EXISTS dead_code_candidates (
  id BIGSERIAL PRIMARY KEY,
  file_path TEXT,
  line_no INT,
  item_name TEXT,
  item_type TEXT,
  confidence INT,
  discovered_at TIMESTAMPTZ DEFAULT NOW(),
  status TEXT DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_dead_code_discovered ON dead_code_candidates(discovered_at DESC);

CREATE TABLE IF NOT EXISTS vulnerability_findings (
  id BIGSERIAL PRIMARY KEY,
  package TEXT,
  current_version TEXT,
  cve TEXT,
  severity TEXT,
  fix_version TEXT,
  discovered_at TIMESTAMPTZ DEFAULT NOW(),
  status TEXT DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_vuln_discovered ON vulnerability_findings(discovered_at DESC);
