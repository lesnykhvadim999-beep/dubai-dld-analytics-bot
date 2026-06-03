-- ============================================================================
-- Layer 13 — Dubai Market World Model
-- PHASE BM Agent I
-- ============================================================================
-- 4 core tables that describe the Dubai real estate market as a
-- graph of entities + time-series + relationships + pre-trained models.
--
-- Idempotent: safe to re-run.
-- ============================================================================

-- 1. market_entities ----------------------------------------------------------
-- Universal entity table: areas, buildings, property_types, developer_brands.
-- parent_id allows hierarchy (building -> area, area -> emirate).
-- embedding is reserved for pgvector if installed; column kept as bytea fallback.

CREATE TABLE IF NOT EXISTS market_entities (
    id              BIGSERIAL PRIMARY KEY,
    entity_type     TEXT NOT NULL,          -- 'area' | 'building' | 'property_type' | 'developer'
    name            TEXT NOT NULL,
    parent_id       BIGINT REFERENCES market_entities(id) ON DELETE SET NULL,
    attributes      JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding       BYTEA,                  -- optional dense vector (384-d float32)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (entity_type, name)
);
CREATE INDEX IF NOT EXISTS ix_mwm_entities_type  ON market_entities(entity_type);
CREATE INDEX IF NOT EXISTS ix_mwm_entities_parent ON market_entities(parent_id);
CREATE INDEX IF NOT EXISTS ix_mwm_entities_attr  ON market_entities USING GIN (attributes);

-- 2. market_state_snapshots ---------------------------------------------------
-- Full snapshot of entity state at a point in time. Used for historical replay
-- and as training data for Prophet (one row per entity per day/week).

CREATE TABLE IF NOT EXISTS market_state_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    entity_id       BIGINT NOT NULL REFERENCES market_entities(id) ON DELETE CASCADE,
    captured_at     TIMESTAMPTZ NOT NULL,
    metrics         JSONB NOT NULL,         -- {price_per_sqft, supply, demand, liquidity_tier, volatility, sentiment, ...}
    source          TEXT,                   -- 'resale_listings' | 'dld_archive' | 'behavior' | 'manual'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (entity_id, captured_at, source)
);
CREATE INDEX IF NOT EXISTS ix_mwm_snap_entity_time
    ON market_state_snapshots(entity_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS ix_mwm_snap_time
    ON market_state_snapshots(captured_at DESC);

-- 3. market_relationships -----------------------------------------------------
-- Generic typed edges. relation_type examples:
--   'area_contains_building', 'building_by_developer',
--   'area_near_metro', 'area_near_mall', 'area_substitute'

CREATE TABLE IF NOT EXISTS market_relationships (
    id              BIGSERIAL PRIMARY KEY,
    from_id         BIGINT NOT NULL REFERENCES market_entities(id) ON DELETE CASCADE,
    to_id           BIGINT NOT NULL REFERENCES market_entities(id) ON DELETE CASCADE,
    relation_type   TEXT NOT NULL,
    strength        DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    attributes      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (from_id, to_id, relation_type)
);
CREATE INDEX IF NOT EXISTS ix_mwm_rel_from ON market_relationships(from_id, relation_type);
CREATE INDEX IF NOT EXISTS ix_mwm_rel_to   ON market_relationships(to_id, relation_type);

-- 4. market_models ------------------------------------------------------------
-- Pickled Prophet (or fallback) models keyed by (entity, metric).
-- Re-trained weekly by builder.py. model_blob is BYTEA (pickle protocol 4).

CREATE TABLE IF NOT EXISTS market_models (
    id              BIGSERIAL PRIMARY KEY,
    entity_id       BIGINT NOT NULL REFERENCES market_entities(id) ON DELETE CASCADE,
    metric          TEXT NOT NULL,           -- 'price_per_sqft' | 'supply' | 'demand' | ...
    algo            TEXT NOT NULL DEFAULT 'prophet',
    model_blob      BYTEA NOT NULL,
    trained_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    rmse            DOUBLE PRECISION,
    samples         INTEGER,
    horizon_days    INTEGER DEFAULT 365,
    meta            JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (entity_id, metric, algo)
);
CREATE INDEX IF NOT EXISTS ix_mwm_models_entity ON market_models(entity_id);
CREATE INDEX IF NOT EXISTS ix_mwm_models_trained ON market_models(trained_at DESC);

-- ============================================================================
-- End schema
-- ============================================================================
