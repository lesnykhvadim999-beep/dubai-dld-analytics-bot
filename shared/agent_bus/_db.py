"""Shared DSN resolver + connection helpers for agent_bus."""
from __future__ import annotations

import os
from typing import Optional

try:
    import psycopg2
    import psycopg2.extras
    _PG_OK = True
except Exception:
    _PG_OK = False

_DSN_CANDIDATES = (
    "INTELLIGENCE_DATABASE_URL",
    "RESALE_DATABASE_URL",
    "DATABASE_URL",
    "BEHAVIOR_DATABASE_URL",
)


def is_available() -> bool:
    return _PG_OK and _dsn() is not None


def _dsn() -> Optional[str]:
    for k in _DSN_CANDIDATES:
        v = os.environ.get(k)
        if v:
            return v
    return None


def connect(autocommit: bool = True):
    dsn = _dsn()
    if not dsn or not _PG_OK:
        return None
    conn = psycopg2.connect(dsn, connect_timeout=5)
    conn.autocommit = autocommit
    return conn
