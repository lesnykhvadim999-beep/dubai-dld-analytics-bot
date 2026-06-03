-- Phase BM Layer 10 — Proactive Agentic Behavior
-- Target DB: RESALE_DATABASE_URL (railway / intelligence)
-- Idempotent.

CREATE TABLE IF NOT EXISTS proactive_triggers (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL,
  bot_name TEXT NOT NULL,
  trigger_type TEXT NOT NULL CHECK (trigger_type IN
    ('new_listing_match','price_drop','roi_change','reengagement','custom')),
  condition_jsonb JSONB NOT NULL DEFAULT '{}'::jsonb,
  cooldown_hours INT DEFAULT 24,
  enabled BOOLEAN DEFAULT TRUE,
  last_fired TIMESTAMPTZ,
  fire_count INT DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_proactive_triggers_active
  ON proactive_triggers(enabled, last_fired) WHERE enabled = TRUE;
CREATE INDEX IF NOT EXISTS idx_proactive_triggers_user
  ON proactive_triggers(user_id, bot_name);
CREATE INDEX IF NOT EXISTS idx_proactive_triggers_type
  ON proactive_triggers(trigger_type, enabled);

-- Notifications sent (audit + rate-limit source of truth)
CREATE TABLE IF NOT EXISTS proactive_notifications (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL,
  bot_name TEXT NOT NULL,
  trigger_id BIGINT REFERENCES proactive_triggers(id) ON DELETE SET NULL,
  trigger_type TEXT,
  payload JSONB,
  message_preview TEXT,
  sent_ts TIMESTAMPTZ DEFAULT NOW(),
  delivery_status TEXT DEFAULT 'sent',  -- sent | failed | blocked | opted_out
  error TEXT,
  clicked BOOLEAN DEFAULT FALSE,
  click_ts TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_proactive_notif_user_ts
  ON proactive_notifications(user_id, sent_ts DESC);
CREATE INDEX IF NOT EXISTS idx_proactive_notif_bot_ts
  ON proactive_notifications(bot_name, sent_ts DESC);

-- Opt-out / preference state
CREATE TABLE IF NOT EXISTS proactive_opt_out (
  user_id BIGINT NOT NULL,
  bot_name TEXT NOT NULL,
  reduced BOOLEAN DEFAULT FALSE,         -- "less notifications" pressed
  blocked BOOLEAN DEFAULT FALSE,         -- full opt-out
  blocked_types TEXT[],                  -- specific trigger_types to mute
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (user_id, bot_name)
);

-- Scheduler run log
CREATE TABLE IF NOT EXISTS proactive_scheduler_runs (
  id BIGSERIAL PRIMARY KEY,
  started_at TIMESTAMPTZ DEFAULT NOW(),
  finished_at TIMESTAMPTZ,
  triggers_evaluated INT DEFAULT 0,
  triggers_matched INT DEFAULT 0,
  notifications_sent INT DEFAULT 0,
  notifications_blocked INT DEFAULT 0,
  errors INT DEFAULT 0,
  details JSONB
);

-- Helper: notifications in last 24h per user (rate-limit view)
CREATE OR REPLACE VIEW proactive_user_24h_rate AS
SELECT
  user_id,
  bot_name,
  COUNT(*) AS notifications_24h,
  MAX(sent_ts) AS last_sent
FROM proactive_notifications
WHERE sent_ts > NOW() - INTERVAL '24 hours'
  AND delivery_status = 'sent'
GROUP BY user_id, bot_name;
