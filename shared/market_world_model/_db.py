"""DB connection helper for Market World Model.

Single source of truth for the DSN. Allows override via env (DATABASE_URL,
RESALE_DB_URL) so the same module works on Railway and locally.
"""
from __future__ import annotations
import os
import psycopg2
import psycopg2.extras

_FALLBACK_DSN = (
    os.environ.get("LIVE_DATABASE_URL") or os.environ.get("DATABASE_URL") or "",
    "@tramway.proxy.rlwy.net:23228/railway"
)


def get_dsn() -> str:
    return (
        os.getenv("MWM_DB_URL")
        or os.getenv("RESALE_DB_URL")
        or os.getenv("DATABASE_URL")
        or _FALLBACK_DSN
    )


def connect():
    """Return a new psycopg2 connection (caller closes)."""
    return psycopg2.connect(get_dsn())


def dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
