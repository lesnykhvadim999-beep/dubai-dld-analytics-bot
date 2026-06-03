-- knowledge_graph migrations (idempotent)
-- Level 5: Cross-bot Knowledge Graph with pgvector semantic search

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS knowledge_graph (
  id BIGSERIAL PRIMARY KEY,
  category TEXT NOT NULL,
  source_bot TEXT,
  payload JSONB NOT NULL,
  description TEXT,
  embedding vector(768),
  upvotes INT DEFAULT 0,
  downvotes INT DEFAULT 0,
  usage_count BIGINT DEFAULT 0,
  validated BOOLEAN DEFAULT false,
  status TEXT DEFAULT 'pending',
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  meta JSONB
);

CREATE INDEX IF NOT EXISTS kg_cat_status_created_idx
  ON knowledge_graph(category, status, created_at DESC);
CREATE INDEX IF NOT EXISTS kg_source_created_idx
  ON knowledge_graph(source_bot, created_at DESC);

-- ivfflat needs data to build; create lazily — skip if 0 rows
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM knowledge_graph LIMIT 1) THEN
    BEGIN
      EXECUTE 'CREATE INDEX IF NOT EXISTS kg_emb_ivfflat_idx ON knowledge_graph USING ivfflat (embedding vector_l2_ops) WITH (lists=50)';
    EXCEPTION WHEN OTHERS THEN NULL;
    END;
  END IF;
END$$;

-- Unique guard for alias entries (category + lower(alias key) in payload)
CREATE UNIQUE INDEX IF NOT EXISTS kg_alias_unique_idx
  ON knowledge_graph (category, ((payload->>'key')))
  WHERE payload ? 'key';

CREATE TABLE IF NOT EXISTS knowledge_usage_log (
  id BIGSERIAL PRIMARY KEY,
  knowledge_id BIGINT REFERENCES knowledge_graph(id) ON DELETE CASCADE,
  bot_name TEXT,
  user_id BIGINT,
  used_at TIMESTAMPTZ DEFAULT NOW(),
  context TEXT,
  successful BOOLEAN
);

CREATE INDEX IF NOT EXISTS kg_usage_kid_idx ON knowledge_usage_log(knowledge_id, used_at DESC);
CREATE INDEX IF NOT EXISTS kg_usage_bot_idx ON knowledge_usage_log(bot_name, used_at DESC);

-- Approval audit
CREATE TABLE IF NOT EXISTS knowledge_approval_log (
  id BIGSERIAL PRIMARY KEY,
  knowledge_id BIGINT REFERENCES knowledge_graph(id) ON DELETE CASCADE,
  action TEXT NOT NULL,  -- approve | reject | deprecate | edit
  actor TEXT,
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
