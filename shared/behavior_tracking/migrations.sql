-- Phase BL — Behavior Tracking + User Feedback
-- Target: resale DB (RESALE_DATABASE_URL / DATABASE_URL)
-- Idempotent: safe to run multiple times.

CREATE TABLE IF NOT EXISTS bot_interactions (
  id BIGSERIAL PRIMARY KEY,
  bot_name TEXT NOT NULL,
  user_id BIGINT,
  ts TIMESTAMPTZ DEFAULT NOW(),
  user_message TEXT,
  user_message_type TEXT,
  bot_response_preview TEXT,
  query_type TEXT,
  query_params JSONB,
  latency_ms INT,
  result_count INT,
  outcome TEXT,
  error_msg TEXT,
  session_id TEXT,
  language TEXT,
  meta JSONB
);

CREATE INDEX IF NOT EXISTS idx_bot_interactions_bot_ts
  ON bot_interactions(bot_name, ts DESC);
CREATE INDEX IF NOT EXISTS idx_bot_interactions_user_ts
  ON bot_interactions(user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_bot_interactions_outcome_ts
  ON bot_interactions(outcome, ts DESC) WHERE outcome != 'success';

CREATE TABLE IF NOT EXISTS bot_feedback (
  id BIGSERIAL PRIMARY KEY,
  interaction_id BIGINT REFERENCES bot_interactions(id) ON DELETE CASCADE,
  user_id BIGINT,
  bot_name TEXT,
  rating SMALLINT,
  rating_emoji TEXT,
  feedback_text TEXT,
  ts TIMESTAMPTZ DEFAULT NOW(),
  responded BOOLEAN DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_bot_feedback_bot_rating_ts
  ON bot_feedback(bot_name, rating, ts DESC);

-- Daily aggregates view (rolling 30 days)
CREATE OR REPLACE VIEW bot_health_daily AS
SELECT
  bot_name,
  DATE_TRUNC('day', ts) AS day,
  COUNT(*) AS total_interactions,
  COUNT(DISTINCT user_id) AS unique_users,
  COUNT(*) FILTER (WHERE outcome='success') AS successes,
  COUNT(*) FILTER (WHERE outcome='empty') AS empties,
  COUNT(*) FILTER (WHERE outcome='error') AS errors,
  ROUND(100.0 * COUNT(*) FILTER (WHERE outcome='success') / NULLIF(COUNT(*),0), 1) AS success_rate_pct,
  PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY latency_ms) AS p50_latency_ms,
  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_latency_ms,
  PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY latency_ms) AS p99_latency_ms
FROM bot_interactions
WHERE ts > NOW() - INTERVAL '30 days'
GROUP BY 1, 2;

CREATE OR REPLACE VIEW bot_feedback_summary AS
SELECT
  bot_name,
  DATE_TRUNC('day', ts) AS day,
  COUNT(*) AS total_fb,
  SUM(CASE WHEN rating=1  THEN 1 ELSE 0 END) AS positive,
  SUM(CASE WHEN rating=-1 THEN 1 ELSE 0 END) AS negative,
  SUM(CASE WHEN rating=0  THEN 1 ELSE 0 END) AS neutral,
  ROUND(100.0 * SUM(CASE WHEN rating=1 THEN 1 ELSE 0 END) / NULLIF(COUNT(*),0), 1) AS satisfaction_pct
FROM bot_feedback
GROUP BY 1, 2;
