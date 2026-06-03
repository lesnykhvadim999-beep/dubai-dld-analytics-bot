"""DSN env-only resolver for the optimizer package.

CRITICAL: never hardcode DSN — pre-commit hook check_safety rejects it.
Only env vars are honoured. Returns None if nothing usable in env.
"""
from __future__ import annotations

import os
from typing import Optional

try:
    import psycopg2  # type: ignore
    import psycopg2.extras  # type: ignore
    _PG_OK = True
except Exception:
    _PG_OK = False

# Match the rest of the ecosystem (agent_bus/_db.py) but stick to the trio
# requested by the spec — LIVE / DATABASE_URL / INTELLIGENCE.
_DSN_CANDIDATES = (
    "LIVE_DATABASE_URL",
    "INTELLIGENCE_DATABASE_URL",
    "RESALE_DATABASE_URL",
    "DATABASE_URL",
)


def is_available() -> bool:
    return _PG_OK and dsn() is not None


def dsn() -> Optional[str]:
    for k in _DSN_CANDIDATES:
        v = os.environ.get(k)
        if v:
            return v
    return None


def connect(autocommit: bool = True):
    """Return a psycopg2 connection or None if env/driver missing."""
    d = dsn()
    if not d or not _PG_OK:
        return None
    conn = psycopg2.connect(d, connect_timeout=8)
    conn.autocommit = autocommit
    ensure_schema(conn)
    return conn


_SCHEMA_APPLIED = False


def ensure_schema(conn) -> None:
    """Idempotently apply schema.sql once per process.  No-op on errors."""
    global _SCHEMA_APPLIED
    if _SCHEMA_APPLIED or conn is None:
        return
    try:
        import os as _os
        schema_path = _os.path.join(_os.path.dirname(__file__), "schema.sql")
        if not _os.path.exists(schema_path):
            _SCHEMA_APPLIED = True
            return
        with open(schema_path, "r", encoding="utf-8") as f:
            sql = f.read()
        with conn.cursor() as cur:
            cur.execute(sql)
        _SCHEMA_APPLIED = True
    except Exception:
        # next call will retry; never block the caller on schema apply
        pass
