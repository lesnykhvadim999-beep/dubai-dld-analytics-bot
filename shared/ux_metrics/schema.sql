-- PHASE BO O3 RE-LAUNCH — UX metrics schema.
-- Idempotent: safe to re-apply on every boot.

CREATE TABLE IF NOT EXISTS ux_events (
  id           BIGSERIAL PRIMARY KEY,
  bot_name     TEXT,
  user_id      BIGINT,
  event_type   TEXT,
  properties   JSONB,
  variant      TEXT,
  occurred_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ux_events_bot_time
  ON ux_events(bot_name, event_type, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_ux_events_user
  ON ux_events(user_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_ux_events_variant
  ON ux_events(variant, occurred_at DESC) WHERE variant IS NOT NULL;

CREATE TABLE IF NOT EXISTS ux_friction_log (
  id            BIGSERIAL PRIMARY KEY,
  bot_name      TEXT,
  user_id       BIGINT,
  friction_type TEXT,
  context       JSONB,
  detected_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ux_friction_bot_time
  ON ux_friction_log(bot_name, friction_type, detected_at DESC);

-- Opt-out registry. user_id+bot_name unique.
CREATE TABLE IF NOT EXISTS ux_opt_outs (
  user_id    BIGINT  NOT NULL,
  bot_name   TEXT    NOT NULL,
  opted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, bot_name)
);

-- Auto-rollback decision audit log
CREATE TABLE IF NOT EXISTS ux_rollback_log (
  id              BIGSERIAL PRIMARY KEY,
  feature_name    TEXT NOT NULL,
  metric_name     TEXT,
  variant_new     TEXT,
  variant_old     TEXT,
  delta_pct       NUMERIC,
  decision        TEXT,                     -- 'rollback' | 'hold' | 'ok'
  consecutive_bad INT,
  detail          JSONB,
  decided_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ux_rollback_feature
  ON ux_rollback_log(feature_name, decided_at DESC);
