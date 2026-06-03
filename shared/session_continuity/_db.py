"""DSN helper for session_continuity (same DB as agent_bus)."""
from __future__ import annotations

import os
from typing import Optional

try:
    import psycopg2
    _PG_OK = True
except Exception:
    _PG_OK = False

_DSN_CANDIDATES = (
    "INTELLIGENCE_DATABASE_URL",
    "RESALE_DATABASE_URL",
    "DATABASE_URL",
)


def _dsn() -> Optional[str]:
    for k in _DSN_CANDIDATES:
        v = os.environ.get(k)
        if v:
            return v
    return None


def is_available() -> bool:
    return _PG_OK and _dsn() is not None


def connect():
    if not _PG_OK:
        return None
    dsn = _dsn()
    if not dsn:
        return None
    conn = psycopg2.connect(dsn, connect_timeout=5)
    conn.autocommit = True
    return conn
