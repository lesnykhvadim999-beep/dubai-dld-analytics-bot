-- Phase BM Layer 15 — Meta-Learning
-- Target: intelligence DB (RESALE_DATABASE_URL / DATABASE_URL)
-- Idempotent.

CREATE TABLE IF NOT EXISTS meta_learning_decisions (
  id BIGSERIAL PRIMARY KEY,
  decided_at TIMESTAMPTZ DEFAULT NOW(),
  task_type TEXT NOT NULL,                    -- extraction|summarization|reasoning|classification|generation
  chosen_strategy TEXT NOT NULL,              -- e.g. "provider:cerebras" / "prompt:B" / "cache:fresh"
  strategy_family TEXT NOT NULL,              -- provider|prompt|cache|verbosity
  context_features JSONB DEFAULT '{}'::jsonb, -- {user_lang, input_len, history_size, ...}
  outcome_score NUMERIC,                      -- 0..1, NULL until feedback recorded
  outcome_label TEXT,                         -- success|fail|partial|unknown
  llm_provider_used TEXT,
  latency_ms INT,
  cost_usd NUMERIC DEFAULT 0,                 -- 0 for all free tier
  bot_name TEXT,
  user_id BIGINT,
  feedback_recorded_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_mld_family_task
  ON meta_learning_decisions(strategy_family, task_type, decided_at DESC);
CREATE INDEX IF NOT EXISTS idx_mld_strategy_ts
  ON meta_learning_decisions(chosen_strategy, decided_at DESC);
CREATE INDEX IF NOT EXISTS idx_mld_bot
  ON meta_learning_decisions(bot_name, decided_at DESC);

CREATE TABLE IF NOT EXISTS meta_strategy_weights (
  id BIGSERIAL PRIMARY KEY,
  strategy_family TEXT NOT NULL,
  task_type TEXT NOT NULL,
  strategy TEXT NOT NULL,
  trials INT DEFAULT 0,
  wins INT DEFAULT 0,
  avg_score NUMERIC DEFAULT 0.5,
  avg_latency_ms NUMERIC DEFAULT 0,
  weight NUMERIC DEFAULT 1.0,                 -- updated weekly by optimizer
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(strategy_family, task_type, strategy)
);

CREATE INDEX IF NOT EXISTS idx_msw_family_task
  ON meta_strategy_weights(strategy_family, task_type);
