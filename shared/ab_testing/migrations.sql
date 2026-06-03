-- Phase BL Level 4 — A/B Testing Framework
-- Target: resale DB. Idempotent.

CREATE TABLE IF NOT EXISTS ab_experiments (
  id BIGSERIAL PRIMARY KEY,
  experiment_key TEXT UNIQUE NOT NULL,
  bot_name TEXT,
  description TEXT,
  variants JSONB NOT NULL,                 -- {"A": "old", "B": "new"}
  traffic_split JSONB DEFAULT '{"A": 0.5, "B": 0.5}'::jsonb,
  default_variant TEXT DEFAULT 'A',
  started_at TIMESTAMPTZ DEFAULT NOW(),
  ended_at TIMESTAMPTZ,
  winner TEXT,
  status TEXT DEFAULT 'running',           -- running|paused|completed|abandoned
  meta JSONB
);

CREATE TABLE IF NOT EXISTS ab_assignments (
  experiment_id BIGINT REFERENCES ab_experiments(id) ON DELETE CASCADE,
  user_id BIGINT NOT NULL,
  variant TEXT NOT NULL,
  assigned_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (experiment_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_ab_assign_exp ON ab_assignments(experiment_id);

CREATE TABLE IF NOT EXISTS ab_outcomes (
  id BIGSERIAL PRIMARY KEY,
  experiment_id BIGINT REFERENCES ab_experiments(id) ON DELETE CASCADE,
  user_id BIGINT,
  variant TEXT,
  outcome_type TEXT,                       -- click|conversion|follow_up|positive_feedback|negative_feedback
  outcome_value NUMERIC DEFAULT 1.0,
  ts TIMESTAMPTZ DEFAULT NOW(),
  meta JSONB
);

CREATE INDEX IF NOT EXISTS idx_ab_out_exp_var_ts
  ON ab_outcomes(experiment_id, variant, ts DESC);
CREATE INDEX IF NOT EXISTS idx_ab_out_exp_type
  ON ab_outcomes(experiment_id, outcome_type);

-- View: counts + outcome rates per variant
CREATE OR REPLACE VIEW ab_results_summary AS
SELECT
  e.id AS experiment_id,
  e.experiment_key,
  e.status,
  a.variant,
  COUNT(DISTINCT a.user_id) AS users_assigned,
  COUNT(o.id) AS total_outcomes,
  COUNT(DISTINCT o.user_id) FILTER (WHERE o.outcome_type='follow_up') AS users_with_follow_up,
  COUNT(o.id) FILTER (WHERE o.outcome_type='positive_feedback') AS positive_fb,
  COUNT(o.id) FILTER (WHERE o.outcome_type='negative_feedback') AS negative_fb,
  COUNT(o.id) FILTER (WHERE o.outcome_type='conversion') AS conversions
FROM ab_experiments e
JOIN ab_assignments a ON a.experiment_id = e.id
LEFT JOIN ab_outcomes o ON o.experiment_id = e.id AND o.user_id = a.user_id
GROUP BY e.id, e.experiment_key, e.status, a.variant;
