-- Layer 17: Causal Inference Engine (DoWhy + EconML)
-- Mathematical counterpart to Layer 11 (LLM reasoning).
-- Run against Intelligence DB (Railway).

CREATE TABLE IF NOT EXISTS causal_studies (
    id                  SERIAL PRIMARY KEY,
    study_name          TEXT UNIQUE NOT NULL,
    treatment_var       TEXT NOT NULL,
    outcome_var         TEXT NOT NULL,
    confounders         TEXT[]            NOT NULL DEFAULT '{}',
    ate                 DOUBLE PRECISION,            -- Average Treatment Effect
    ate_lower_ci        DOUBLE PRECISION,            -- 95 % CI lower
    ate_upper_ci        DOUBLE PRECISION,            -- 95 % CI upper
    effect_unit         TEXT,                        -- 'AED/sqft', '% yield', '% premium', ...
    p_value             DOUBLE PRECISION,
    sample_size         INTEGER,
    method              TEXT,                        -- 'backdoor.linear_regression' | 'EconML.LinearDML' | ...
    refutation_passed   BOOLEAN,                     -- DoWhy refutation test result
    dag_blob            JSONB,                       -- causal graph (nodes / edges)
    model_blob          BYTEA,                       -- pickled estimator
    natural_language    TEXT,                        -- Cerebras-generated explanation (RU)
    refreshed_at        TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_causal_studies_name ON causal_studies(study_name);
CREATE INDEX IF NOT EXISTS idx_causal_studies_refreshed ON causal_studies(refreshed_at DESC);


CREATE TABLE IF NOT EXISTS causal_counterfactuals (
    id                SERIAL PRIMARY KEY,
    study_id          INTEGER REFERENCES causal_studies(id) ON DELETE CASCADE,
    scenario_jsonb    JSONB NOT NULL,                -- {"treatment": 1, "confounders": {...}}
    predicted_outcome DOUBLE PRECISION,
    predicted_lower   DOUBLE PRECISION,
    predicted_upper   DOUBLE PRECISION,
    label             TEXT,                          -- short human label
    computed_at       TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_causal_cf_study ON causal_counterfactuals(study_id);
