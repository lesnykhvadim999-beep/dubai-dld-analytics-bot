-- safety_nets schema — circuit breakers, feature flags, watchdog heartbeats
-- Applied automatically by shared.safety_nets.bootstrap.apply_schema().
-- Idempotent — uses CREATE TABLE IF NOT EXISTS + ALTER TABLE ADD COLUMN IF NOT EXISTS
-- so it co-exists with any prior schema variant on shared DBs.

CREATE TABLE IF NOT EXISTS circuit_breakers (
    name              TEXT PRIMARY KEY,
    state             TEXT NOT NULL DEFAULT 'CLOSED',
    failure_count     INT  NOT NULL DEFAULT 0,
    success_count     INT  NOT NULL DEFAULT 0,
    opened_at         TIMESTAMPTZ,
    last_failure_at   TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE circuit_breakers ADD COLUMN IF NOT EXISTS half_open_attempts INT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS feature_flags (
    name                  TEXT PRIMARY KEY,
    enabled               BOOLEAN NOT NULL DEFAULT TRUE,
    consecutive_failures  INT     NOT NULL DEFAULT 0,
    last_failure_at       TIMESTAMPTZ,
    auto_disabled_at      TIMESTAMPTZ,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE feature_flags ADD COLUMN IF NOT EXISTS note TEXT;

CREATE TABLE IF NOT EXISTS bot_heartbeats (
    bot_name       TEXT PRIMARY KEY,
    last_beat_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    uptime_sec     BIGINT NOT NULL DEFAULT 0
);
ALTER TABLE bot_heartbeats ADD COLUMN IF NOT EXISTS last_alert_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_bot_heartbeats_last_beat ON bot_heartbeats(last_beat_at);
CREATE INDEX IF NOT EXISTS idx_feature_flags_enabled ON feature_flags(enabled);
