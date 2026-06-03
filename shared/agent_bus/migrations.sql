-- PHASE BM Layer 12: Multi-Agent Orchestration via Postgres LISTEN/NOTIFY
-- Idempotent migration. Run on Railway intelligence DB.

CREATE TABLE IF NOT EXISTS agent_events (
    id              BIGSERIAL PRIMARY KEY,
    source_bot      TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_by    JSONB NOT NULL DEFAULT '[]'::jsonb,
    status          TEXT NOT NULL DEFAULT 'pending'  -- pending | processed | failed
);

CREATE INDEX IF NOT EXISTS idx_agent_events_type_created
    ON agent_events (event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_events_status
    ON agent_events (status) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_agent_events_created
    ON agent_events (created_at DESC);

CREATE TABLE IF NOT EXISTS agent_registry (
    agent_name        TEXT PRIMARY KEY,
    subscribes_to     TEXT[] NOT NULL DEFAULT '{}',
    handler_endpoint  TEXT,                            -- optional HTTP webhook, else in-process
    status            TEXT NOT NULL DEFAULT 'active',  -- active | paused | dead
    last_heartbeat    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    meta              JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_agent_registry_status
    ON agent_registry (status);

-- PHASE BM Layer 14: Cross-Bot Conversation Continuity
CREATE TABLE IF NOT EXISTS cross_bot_sessions (
    user_id           BIGSERIAL PRIMARY KEY,
    telegram_id       BIGINT NOT NULL UNIQUE,
    current_context   JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_bot          TEXT,
    last_intent       TEXT,
    last_message_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cross_bot_sessions_tg
    ON cross_bot_sessions (telegram_id);
CREATE INDEX IF NOT EXISTS idx_cross_bot_sessions_last_msg
    ON cross_bot_sessions (last_message_at DESC);

-- Trigger function to NOTIFY on new event insert
CREATE OR REPLACE FUNCTION notify_agent_event() RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify(
        'agent_events',
        json_build_object(
            'id', NEW.id,
            'event_type', NEW.event_type,
            'source_bot', NEW.source_bot
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_agent_events_notify ON agent_events;
CREATE TRIGGER trg_agent_events_notify
    AFTER INSERT ON agent_events
    FOR EACH ROW EXECUTE FUNCTION notify_agent_event();
