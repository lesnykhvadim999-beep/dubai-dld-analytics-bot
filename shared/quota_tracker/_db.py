"""DSN resolution + connection helpers for quota_tracker.

DSN env vars (priority order):
    LIVE_DATABASE_URL  -> DATABASE_URL  -> INTELLIGENCE_DATABASE_URL
"""
from __future__ import annotations

import os
import logging
from typing import Optional

log = logging.getLogger("quota_tracker.db")

_DSN_VARS = ("LIVE_DATABASE_URL", "DATABASE_URL", "INTELLIGENCE_DATABASE_URL")


def get_dsn() -> Optional[str]:
    for name in _DSN_VARS:
        v = os.environ.get(name)
        if v:
            return v
    return None


# ── psycopg sync (used by sync helpers and schema apply) ─────────────────────
def _psycopg():
    try:
        import psycopg2  # type: ignore
        return psycopg2
    except Exception:
        try:
            import psycopg  # type: ignore  # psycopg3
            return psycopg
        except Exception:
            return None


def sync_connect():
    """Returns a sync DB connection or None when DSN/driver missing."""
    dsn = get_dsn()
    if not dsn:
        return None
    drv = _psycopg()
    if drv is None:
        log.warning("quota_tracker: no psycopg/psycopg2 installed; skip")
        return None
    try:
        return drv.connect(dsn)
    except Exception as e:
        log.warning("quota_tracker: sync_connect failed: %s", e)
        return None


# ── async (asyncpg preferred) ────────────────────────────────────────────────
async def async_connect():
    """Returns an asyncpg connection or None."""
    dsn = get_dsn()
    if not dsn:
        return None
    try:
        import asyncpg  # type: ignore
    except Exception:
        return None
    try:
        # asyncpg expects postgres:// not postgresql+psycopg2://
        clean = dsn.replace("postgresql+psycopg2://", "postgresql://").replace(
            "postgres+psycopg://", "postgresql://"
        )
        return await asyncpg.connect(clean, timeout=5)
    except Exception as e:
        log.warning("quota_tracker: async_connect failed: %s", e)
        return None


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS api_usage_log (
  id BIGSERIAL PRIMARY KEY,
  provider TEXT NOT NULL,
  tokens_in INT,
  tokens_out INT,
  latency_ms INT,
  success BOOLEAN,
  bot_name TEXT,
  user_id BIGINT,
  called_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_api_usage_provider_time
  ON api_usage_log(provider, called_at DESC);

CREATE TABLE IF NOT EXISTS quota_limits (
  provider TEXT PRIMARY KEY,
  rpm INT,
  tpm INT,
  rpd INT,
  tpd INT,
  current_window_start TIMESTAMPTZ,
  last_429_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS quota_alerts (
  id BIGSERIAL PRIMARY KEY,
  provider TEXT,
  level TEXT,
  pct_used REAL,
  message TEXT,
  alerted_at TIMESTAMPTZ DEFAULT NOW()
);
"""


def apply_schema() -> bool:
    """Sync DDL apply. Returns True on success."""
    conn = sync_connect()
    if conn is None:
        log.warning("quota_tracker.apply_schema: no DB connection")
        return False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
        return True
    except Exception as e:
        log.error("quota_tracker.apply_schema failed: %s", e)
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def upsert_quota_limit(provider: str, rpm=None, tpm=None, rpd=None, tpd=None) -> bool:
    conn = sync_connect()
    if conn is None:
        return False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO quota_limits (provider, rpm, tpm, rpd, tpd, updated_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (provider) DO UPDATE SET
                      rpm = EXCLUDED.rpm,
                      tpm = EXCLUDED.tpm,
                      rpd = EXCLUDED.rpd,
                      tpd = EXCLUDED.tpd,
                      updated_at = NOW()
                    """,
                    (provider, rpm, tpm, rpd, tpd),
                )
        return True
    except Exception as e:
        log.warning("quota_tracker.upsert_quota_limit(%s) failed: %s", provider, e)
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass
