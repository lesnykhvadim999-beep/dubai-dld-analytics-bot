-- PHASE BN N4 — Autonomous Audit Loop schema
-- Apply to RESALE Postgres (also fine on intelligence DB; pick one — runner
-- uses DSN env-vars in order: RESALE > INTELLIGENCE > LIVE > DATABASE_URL).

CREATE TABLE IF NOT EXISTS public.audit_runs (
  id BIGSERIAL PRIMARY KEY,
  audit_type     TEXT NOT NULL,
  started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at    TIMESTAMPTZ,
  status         TEXT,
  findings_count INT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS audit_runs_type_started_idx
  ON public.audit_runs(audit_type, started_at DESC);

CREATE TABLE IF NOT EXISTS public.audit_findings (
  id BIGSERIAL PRIMARY KEY,
  audit_run_id   BIGINT REFERENCES public.audit_runs(id) ON DELETE CASCADE,
  severity       TEXT,
  category       TEXT,
  description    TEXT,
  affected_files TEXT[],
  discovered_at  TIMESTAMPTZ DEFAULT NOW(),
  resolved_at    TIMESTAMPTZ,
  escalated      BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS audit_findings_run_idx
  ON public.audit_findings(audit_run_id);
CREATE INDEX IF NOT EXISTS audit_findings_severity_idx
  ON public.audit_findings(severity, discovered_at DESC);

CREATE TABLE IF NOT EXISTS public.audit_check_registry (
  check_name      TEXT PRIMARY KEY,
  audit_type      TEXT,
  enabled         BOOLEAN DEFAULT TRUE,
  last_run_at     TIMESTAMPTZ,
  last_finding_at TIMESTAMPTZ
);

-- Seed 10 base checks. Idempotent — re-running this section is safe.
INSERT INTO public.audit_check_registry (check_name, audit_type, enabled) VALUES
  ('check_heartbeat_freshness',   'hourly', TRUE),
  ('check_open_circuit_breakers', 'hourly', TRUE),
  ('check_queue_depth',           'hourly', TRUE),
  ('check_contract_drift',        'daily',  TRUE),
  ('check_cron_freshness',        'daily',  TRUE),
  ('check_disk_size_growth',      'daily',  TRUE),
  ('check_handler_dead_links',    'daily',  TRUE),
  ('check_schema_integrity',      'weekly', TRUE),
  ('check_secrets_compliance',    'weekly', TRUE),
  ('check_brand_compliance',      'weekly', TRUE)
ON CONFLICT (check_name) DO UPDATE
  SET audit_type = EXCLUDED.audit_type;
