-- Phase BM Layer 16 — External Knowledge Integration
-- Target: intelligence DB. Embeddings stored as TEXT (JSON) to avoid pgvector dep;
-- if pgvector is available the column is upgraded by upgrade_to_vector().
-- Idempotent.

CREATE TABLE IF NOT EXISTS external_signals (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,                  -- "rss:arabianbusiness" | "tg:dubai_real_estate_news"
  source_kind TEXT NOT NULL,             -- rss|telegram|github
  signal_type TEXT,                      -- news|trend|listing|market_update|other
  url TEXT,
  title TEXT,
  raw_text TEXT,
  extracted_facts JSONB DEFAULT '{}'::jsonb,  -- {areas:[], price_changes:[], events:[], ...}
  embedding TEXT,                        -- JSON array of floats, len 384
  relevance_score NUMERIC DEFAULT 0,     -- 0..1, set by classifier
  published_at TIMESTAMPTZ,
  fetched_at TIMESTAMPTZ DEFAULT NOW(),
  fingerprint CHAR(40) NOT NULL,         -- sha1 of url+title (dedup)
  language TEXT DEFAULT 'en'
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_external_signals_fingerprint
  ON external_signals(fingerprint);
CREATE INDEX IF NOT EXISTS idx_es_source_ts
  ON external_signals(source, published_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_es_relevance_ts
  ON external_signals(relevance_score DESC, published_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_es_published
  ON external_signals(published_at DESC NULLS LAST);

CREATE TABLE IF NOT EXISTS external_source_state (
  source TEXT PRIMARY KEY,
  last_polled_at TIMESTAMPTZ,
  last_etag TEXT,
  last_modified TEXT,
  errors_in_row INT DEFAULT 0,
  total_fetched INT DEFAULT 0
);
