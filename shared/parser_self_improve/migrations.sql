-- Phase 1 self-improving parser — schema
-- Idempotent: можно запускать многократно.

CREATE TABLE IF NOT EXISTS parser_failed_log (
    id BIGSERIAL PRIMARY KEY,
    listing_id BIGINT,                     -- FK мягкая, без CASCADE (listings.id может удаляться)
    original_text TEXT NOT NULL,
    parsed_result JSONB NOT NULL,
    confidence NUMERIC,
    missing_fields TEXT[],
    created_at TIMESTAMPTZ DEFAULT NOW(),
    cluster_id BIGINT,                     -- заполняется weekly clustering
    resolved BOOLEAN DEFAULT false,
    resolved_at TIMESTAMPTZ,
    text_hash CHAR(64)                     -- sha256(original_text) для дедупликации
);

CREATE INDEX IF NOT EXISTS idx_pfl_cluster_resolved
    ON parser_failed_log(cluster_id, resolved);

CREATE INDEX IF NOT EXISTS idx_pfl_created_at
    ON parser_failed_log(created_at);

-- Полный UNIQUE (без WHERE) — нужен для ON CONFLICT (text_hash).
-- text_hash всегда заполняется в failed_collector.
-- DROP старого partial-index если существует.
DROP INDEX IF EXISTS idx_pfl_text_hash;
CREATE UNIQUE INDEX idx_pfl_text_hash
    ON parser_failed_log(text_hash);

-- Cluster registry (1 cluster per discovery cycle)
CREATE TABLE IF NOT EXISTS parser_pattern_clusters (
    id BIGSERIAL PRIMARY KEY,
    discovered_at TIMESTAMPTZ DEFAULT NOW(),
    size INT NOT NULL,
    centroid_summary TEXT,
    suggested_rule_json JSONB,             -- {"regex": ..., "field": ..., "sample_extractor": ...}
    status VARCHAR(20) DEFAULT 'pending',  -- pending | approved | rejected | applied
    admin_message_id BIGINT,               -- telegram message id для callback ref
    approved_at TIMESTAMPTZ,
    rejected_at TIMESTAMPTZ,
    applied_at TIMESTAMPTZ,
    applied_commit_sha CHAR(40)
);

CREATE INDEX IF NOT EXISTS idx_ppc_status ON parser_pattern_clusters(status);
