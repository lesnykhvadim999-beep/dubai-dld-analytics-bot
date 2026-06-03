-- Disaster Recovery bookkeeping tables (PHASE BO O1).
-- Live in intelligence DB.
CREATE TABLE IF NOT EXISTS backup_runs (
  id BIGSERIAL PRIMARY KEY,
  db_name TEXT NOT NULL,
  backup_type TEXT NOT NULL,             -- daily / weekly / monthly / yearly / schema
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ,
  size_mb REAL,
  storage_url TEXT,                      -- s3://bucket/key OR https://github.com/... OR file://
  storage_backend TEXT,                  -- r2 / b2 / github / local
  encrypted BOOLEAN DEFAULT TRUE,
  status TEXT NOT NULL DEFAULT 'running',-- running / ok / failed
  error_msg TEXT,
  sha256 TEXT
);
CREATE INDEX IF NOT EXISTS idx_backup_runs_started ON backup_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_backup_runs_db_type ON backup_runs(db_name, backup_type, started_at DESC);

CREATE TABLE IF NOT EXISTS restore_verifications (
  id BIGSERIAL PRIMARY KEY,
  backup_run_id BIGINT REFERENCES backup_runs(id) ON DELETE SET NULL,
  verified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  tables_count INT,
  rows_total BIGINT,
  duration_sec INT,
  status TEXT NOT NULL,                  -- ok / failed
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_restore_verif_when ON restore_verifications(verified_at DESC);
