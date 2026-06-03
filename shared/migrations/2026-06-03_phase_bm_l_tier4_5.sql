-- PHASE BM Agent L — Tier 4+5 schema
-- Layers 18-22: Multimodal, Content Pipeline, Virtual Tours, Self-Modify, Continuous Reasoning
-- Дата: 2026-06-03

BEGIN;

-- ============================================================
-- Layer 18: Multimodal Understanding
-- ============================================================
CREATE TABLE IF NOT EXISTS multimodal_inputs (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT      NOT NULL,
    bot          TEXT        NOT NULL,            -- 'resale-bot', 'lead-bot', ...
    input_type   TEXT        NOT NULL,            -- 'photo' | 'voice' | 'document' | 'video'
    file_id      TEXT        NOT NULL,            -- Telegram file_id
    file_unique_id TEXT,
    mime_type    TEXT,
    transcript   TEXT,                            -- voice -> Whisper text
    extracted    JSONB       NOT NULL DEFAULT '{}'::jsonb, -- vision features / parsed doc
    intent       TEXT,                            -- классификация намерения (search/sell/ask/...)
    confidence   NUMERIC(4,3),
    llm_provider TEXT,                            -- 'gemini-flash-2.0' | 'groq-whisper-v3' | ...
    processed_at TIMESTAMPTZ DEFAULT NOW(),
    error        TEXT,
    UNIQUE (bot, file_unique_id)
);

CREATE INDEX IF NOT EXISTS idx_multimodal_user ON multimodal_inputs(user_id, processed_at DESC);
CREATE INDEX IF NOT EXISTS idx_multimodal_bot  ON multimodal_inputs(bot, input_type, processed_at DESC);
CREATE INDEX IF NOT EXISTS idx_multimodal_intent ON multimodal_inputs(intent) WHERE intent IS NOT NULL;

-- ============================================================
-- Layer 19: Auto-Generated Content Pipeline
-- ============================================================
CREATE TABLE IF NOT EXISTS content_pipeline_queue (
    id            BIGSERIAL PRIMARY KEY,
    kind          TEXT  NOT NULL,                 -- 'daily_market' | 'weekly_top' | 'monthly_report'
    target_chat   TEXT  NOT NULL,                 -- '@channel_username' | chat_id
    title         TEXT,
    body_md       TEXT  NOT NULL,
    media_paths   JSONB DEFAULT '[]'::jsonb,      -- список путей картинок/видео/аудио
    payload       JSONB DEFAULT '{}'::jsonb,      -- raw данные для дебага
    status        TEXT  NOT NULL DEFAULT 'pending', -- pending|approved|rejected|published|failed
    review_admin  BIGINT,
    review_note   TEXT,
    scheduled_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW(),
    published_at  TIMESTAMPTZ,
    error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_content_status ON content_pipeline_queue(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_content_kind   ON content_pipeline_queue(kind, created_at DESC);

-- ============================================================
-- Layer 20: Virtual Property Tours
-- ============================================================
CREATE TABLE IF NOT EXISTS virtual_tours (
    id           BIGSERIAL PRIMARY KEY,
    listing_id   TEXT NOT NULL,
    bot          TEXT NOT NULL,
    rooms        JSONB NOT NULL DEFAULT '[]'::jsonb, -- [{name, prompt, image_path}]
    script_md    TEXT,                                -- сторителлинг
    audio_path   TEXT,                                -- Edge-TTS озвучка
    video_path   TEXT,                                -- MoviePy slideshow
    cover_image  TEXT,
    cache_hits   INTEGER DEFAULT 0,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    UNIQUE (bot, listing_id)
);

CREATE INDEX IF NOT EXISTS idx_tours_listing ON virtual_tours(listing_id);

-- ============================================================
-- Layer 21: Self-Modifying Code
-- ============================================================
CREATE TABLE IF NOT EXISTS code_proposals (
    id                  BIGSERIAL PRIMARY KEY,
    bug_id              TEXT,                            -- ссылка на bug_kb (B055+)
    alert_id            BIGINT,                          -- ссылка на auto_audit alert
    target_repo         TEXT NOT NULL,                   -- 'resale-bot' | 'shared' | ...
    target_files        JSONB NOT NULL DEFAULT '[]'::jsonb,
    title               TEXT NOT NULL,
    rationale           TEXT,                            -- что и зачем
    proposed_diff       TEXT NOT NULL,                   -- unified diff
    sandbox_test_result JSONB,                           -- pytest + smoke
    sandbox_passed      BOOLEAN,
    status              TEXT NOT NULL DEFAULT 'pending', -- pending|approved|rejected|applied|reverted
    admin_decision_by   BIGINT,
    admin_decision_at   TIMESTAMPTZ,
    applied_commit_sha  TEXT,
    applied_at          TIMESTAMPTZ,
    revert_reason       TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT proposal_status_valid CHECK (status IN ('pending','approved','rejected','applied','reverted','sandbox_failed'))
);

CREATE INDEX IF NOT EXISTS idx_proposals_status ON code_proposals(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_proposals_repo   ON code_proposals(target_repo);

-- ============================================================
-- Layer 22: Continuous Reasoning Loop
-- ============================================================
CREATE TABLE IF NOT EXISTS continuous_thoughts (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    bot          TEXT   NOT NULL,
    trigger_msg  TEXT,                            -- исходный запрос юзера
    thought      TEXT   NOT NULL,                 -- что бот "додумал"
    followup_msg TEXT,                            -- готовый текст follow-up
    fire_at      TIMESTAMPTZ NOT NULL,
    sent_at      TIMESTAMPTZ,
    status       TEXT NOT NULL DEFAULT 'queued',  -- queued|sent|skipped|opted_out|expired
    skip_reason  TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_thoughts_fire ON continuous_thoughts(status, fire_at) WHERE status = 'queued';
CREATE INDEX IF NOT EXISTS idx_thoughts_user ON continuous_thoughts(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS continuous_optout (
    user_id    BIGINT PRIMARY KEY,
    bot        TEXT,
    opted_at   TIMESTAMPTZ DEFAULT NOW()
);

COMMIT;
