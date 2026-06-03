-- ============================================================================
-- idempotency_keys
-- ----------------------------------------------------------------------------
-- Cache for write-side idempotency. See stability.py::idempotent_write().
--
-- Run on EVERY bot DB that performs user-initiated writes:
--   - resale-bot  (listings, saved searches)
--   - lead-bot    (leads)
--   - roi-bot     (roi calculations)
--   - channel-bot (subscription activations)
--
-- Idempotent: safe to run multiple times.
-- ============================================================================

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key        TEXT PRIMARY KEY,
    response   JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Cleanup index (delete rows older than 24h)
CREATE INDEX IF NOT EXISTS idempotency_keys_created_at_idx
    ON idempotency_keys (created_at);

-- Optional: scheduled cleanup via pg_cron (uncomment if extension installed)
-- SELECT cron.schedule(
--     'idempotency-keys-cleanup',
--     '*/15 * * * *',
--     $$ DELETE FROM idempotency_keys
--        WHERE created_at < now() - INTERVAL '24 hours' $$
-- );

-- If pg_cron is unavailable (Railway default), the application itself should
-- call stability.cleanup_idempotency_keys(conn) once per hour from a
-- background thread or scheduled tick.

COMMENT ON TABLE  idempotency_keys IS
  'Write-side idempotency cache; populated by stability.idempotent_write().';
COMMENT ON COLUMN idempotency_keys.key IS
  'sha1(bot|user_id|action|time_bucket|extra)[:24] — see make_idempotency_key().';
COMMENT ON COLUMN idempotency_keys.response IS
  'JSON-encoded return value of the wrapped write; replayed on duplicates.';
COMMENT ON COLUMN idempotency_keys.created_at IS
  'GC cutoff. Rows older than 24h are safe to delete.';
