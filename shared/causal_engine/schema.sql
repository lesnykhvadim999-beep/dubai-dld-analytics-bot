-- Layer 11: Causal Reasoning Engine
-- Tables for causal facts and chains.
-- Run against Intelligence DB (Railway).

CREATE TABLE IF NOT EXISTS causal_facts (
    id              SERIAL PRIMARY KEY,
    cause_text      TEXT NOT NULL,
    effect_text     TEXT NOT NULL,
    evidence_source TEXT NOT NULL CHECK (evidence_source IN ('DLD_data','news','expert','telegram','rss','seed')),
    confidence      REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    domain          TEXT,                 -- 'price' | 'roi' | 'supply' | 'rent' | 'demand' | 'macro'
    areas           TEXT[],               -- optional restriction (NULL = all)
    time_horizon    TEXT,                 -- 'short' (<3mo) | 'mid' (3-12mo) | 'long' (>12mo)
    supporting_examples JSONB DEFAULT '[]'::jsonb,
    source_url      TEXT,
    created_at      TIMESTAMP DEFAULT NOW(),
    verified_at     TIMESTAMP,
    last_validated  TIMESTAMP,
    validation_score REAL,                -- backtest result 0..1
    UNIQUE (cause_text, effect_text)
);

CREATE INDEX IF NOT EXISTS idx_causal_facts_domain ON causal_facts(domain);
CREATE INDEX IF NOT EXISTS idx_causal_facts_areas ON causal_facts USING GIN (areas);
CREATE INDEX IF NOT EXISTS idx_causal_facts_confidence ON causal_facts(confidence DESC);

CREATE TABLE IF NOT EXISTS causal_chains (
    id                SERIAL PRIMARY KEY,
    query_pattern     TEXT NOT NULL,       -- e.g. 'why_roi_down:Marina'
    question_text     TEXT,                -- raw question for debugging
    chain_steps       JSONB NOT NULL,      -- [{"fact_id":1,"cause":..,"effect":..,"conf":..}, ...]
    final_explanation TEXT NOT NULL,
    llm_provider      TEXT,                -- 'cerebras' | 'groq' | ...
    features_snapshot JSONB,               -- input features used (price,supply,tx)
    user_id           BIGINT,
    bot_source        TEXT,                -- 'resale' | 'analytics' | 'roi'
    last_used         TIMESTAMP DEFAULT NOW(),
    use_count         INT DEFAULT 1,
    created_at        TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_causal_chains_pattern ON causal_chains(query_pattern);
CREATE INDEX IF NOT EXISTS idx_causal_chains_last_used ON causal_chains(last_used DESC);

-- learning log: новые гипотезы, добытые learner.py
CREATE TABLE IF NOT EXISTS causal_learning_log (
    id           SERIAL PRIMARY KEY,
    source_url   TEXT,
    raw_excerpt  TEXT,
    proposed_cause TEXT,
    proposed_effect TEXT,
    llm_confidence REAL,
    status       TEXT DEFAULT 'pending', -- pending|accepted|rejected
    reviewer_note TEXT,
    created_at   TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_causal_learning_status ON causal_learning_log(status);
