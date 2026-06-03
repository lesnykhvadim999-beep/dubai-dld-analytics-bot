"""Shared DB connection + schema apply helper for safety_nets.

DSN env vars checked in order:
  LIVE_DATABASE_URL, DATABASE_URL, INTELLIGENCE_DATABASE_URL.

Never hardcode DSN — pre-commit hook check_safety blocks it.
"""
from __future__ import annotations
import os
import logging
from typing import Optional

log = logging.getLogger("safety_nets.bootstrap")

_DSN_ENVS = ("LIVE_DATABASE_URL", "DATABASE_URL", "INTELLIGENCE_DATABASE_URL")


def _resolve_dsn() -> Optional[str]:
    for k in _DSN_ENVS:
        v = os.environ.get(k, "").strip()
        if v:
            return v
    return None


def get_conn():
    """Return a new psycopg2 connection or None if unavailable.

    Callers MUST close the connection. Failures never raise — they log and
    return None so safety_nets degrade gracefully (fail-open).
    """
    dsn = _resolve_dsn()
    if not dsn:
        log.warning("safety_nets: no DSN env var set (%s)", ",".join(_DSN_ENVS))
        return None
    try:
        import psycopg2
        return psycopg2.connect(dsn, connect_timeout=5)
    except Exception as e:
        log.warning("safety_nets: connect failed: %s", e)
        return None


def apply_schema() -> bool:
    """Apply schema.sql idempotently. Returns True on success."""
    here = os.path.dirname(os.path.abspath(__file__))
    sql_path = os.path.join(here, "schema.sql")
    if not os.path.isfile(sql_path):
        log.warning("safety_nets: schema.sql missing at %s", sql_path)
        return False
    with open(sql_path, "r", encoding="utf-8") as f:
        sql = f.read()
    conn = get_conn()
    if conn is None:
        return False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
        return True
    except Exception as e:
        log.warning("safety_nets: schema apply failed: %s", e)
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass
