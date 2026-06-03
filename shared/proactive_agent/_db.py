"""shared.proactive_agent._db — DB helpers (shared pattern with user_memory)."""
from __future__ import annotations
import os
import threading
from typing import Optional

try:
    import psycopg2
    import psycopg2.extras
    _PG_OK = True
except Exception:
    _PG_OK = False

_DSN_ENV_CANDIDATES = (
    "RESALE_DATABASE_URL",
    "DATABASE_URL",
    "INTELLIGENCE_DATABASE_URL",
)

_conn_lock = threading.Lock()
_conn = None


def dsn() -> Optional[str]:
    for k in _DSN_ENV_CANDIDATES:
        v = os.environ.get(k)
        if v:
            return v
    return None


def get_conn():
    global _conn
    if not _PG_OK:
        return None
    d = dsn()
    if not d:
        return None
    with _conn_lock:
        try:
            if _conn is None or _conn.closed:
                _conn = psycopg2.connect(d, connect_timeout=8)
                _conn.autocommit = True
            return _conn
        except Exception:
            _conn = None
            return None
