"""DSN resolver + tiny psycopg2 helpers for immune_system.

Strictly env-only — never embed credentials in code. The pre-commit hook
`check_safety` blocks any literal DSN with a password in this repo.
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


def get_dsn() -> str:
    """Return the first non-empty DSN env var (LIVE → DATABASE → INTELLIGENCE).

    Returns "" when no env var is set so callers can no-op cleanly without
    raising on import.
    """
    return (
        os.environ.get("LIVE_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or os.environ.get("INTELLIGENCE_DATABASE_URL")
        or ""
    )


def is_available() -> bool:
    return _PG_OK and bool(get_dsn())


def connect(autocommit: bool = True):
    """Open a short psycopg2 connection or return None when DSN/driver missing.

    Callers must always `if conn is None: return` — immune_system is best-effort
    and must never break a bot if Postgres is unreachable.
    """
    if not is_available():
        return None
    try:
        conn = psycopg2.connect(get_dsn(), connect_timeout=5)
        conn.autocommit = autocommit
        return conn
    except Exception:
        return None
