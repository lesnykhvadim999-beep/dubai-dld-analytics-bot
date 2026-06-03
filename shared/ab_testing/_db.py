"""DB helper for ab_testing (same DSN as resale)."""
from __future__ import annotations

import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras


def get_dsn() -> str:
    dsn = (
        os.environ.get("RESALE_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or os.environ.get("RESALE_DATABASE_URL_PUBLIC")
    )
    if not dsn:
        raise RuntimeError("No DB DSN for ab_testing")
    return dsn


@contextmanager
def conn_cursor(dict_cursor: bool = False):
    conn = psycopg2.connect(get_dsn())
    try:
        cur = conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor if dict_cursor else None
        )
        yield conn, cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            cur.close()
        except Exception:
            pass
        conn.close()


def ensure_schema() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    sql_path = os.path.join(here, "migrations.sql")
    with open(sql_path, "r", encoding="utf-8") as f:
        sql = f.read()
    with conn_cursor() as (_, cur):
        cur.execute(sql)
