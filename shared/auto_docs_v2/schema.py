"""SQL schema for PHASE BO O4 auto_docs_v2."""
from __future__ import annotations
import os
from typing import Optional

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bot_help_texts (
  bot_name TEXT NOT NULL,
  language TEXT NOT NULL DEFAULT 'ru',
  content TEXT NOT NULL,
  generated_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (bot_name, language)
);

CREATE TABLE IF NOT EXISTS docs_inventory (
  doc_path TEXT PRIMARY KEY,
  doc_type TEXT,
  last_generated_at TIMESTAMPTZ DEFAULT NOW(),
  size_bytes INT
);
"""

# DSN env vars only — never hardcode.
# Public *_PUBLIC variants are also accepted so local dev / cron-from-laptop works.
DSN_KEYS = (
    "INTELLIGENCE_DATABASE_URL",
    "RESALE_DATABASE_URL",
    "DATABASE_URL",
    "BEHAVIOR_DATABASE_URL",
    "INTELLIGENCE_DATABASE_URL_PUBLIC",
    "RESALE_DATABASE_URL_PUBLIC",
    "DATABASE_URL_PUBLIC",
)


def get_dsn() -> Optional[str]:
    for k in DSN_KEYS:
        v = os.environ.get(k)
        if v:
            return v
    return None


def apply_schema() -> bool:
    """Apply schema. Returns True on success, False on failure (logged)."""
    import sys
    dsn = get_dsn()
    if not dsn:
        sys.stderr.write("[auto_docs_v2.schema] no DSN env var set; skip\n")
        return False
    try:
        import psycopg2  # type: ignore
    except Exception as e:
        sys.stderr.write(f"[auto_docs_v2.schema] psycopg2 missing: {e}\n")
        return False
    try:
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
        return True
    except Exception as e:
        sys.stderr.write(f"[auto_docs_v2.schema] apply failed: {e}\n")
        return False


def upsert_help(bot_name: str, language: str, content: str) -> bool:
    dsn = get_dsn()
    if not dsn:
        return False
    try:
        import psycopg2  # type: ignore
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO bot_help_texts (bot_name, language, content, generated_at)
                       VALUES (%s, %s, %s, NOW())
                       ON CONFLICT (bot_name, language)
                       DO UPDATE SET content = EXCLUDED.content,
                                     generated_at = NOW()""",
                    (bot_name, language, content),
                )
        return True
    except Exception as e:
        import sys
        sys.stderr.write(f"[auto_docs_v2.schema] upsert_help failed ({bot_name}/{language}): {e}\n")
        return False


def get_help(bot_name: str, language: str) -> Optional[str]:
    dsn = get_dsn()
    if not dsn:
        return None
    try:
        import psycopg2  # type: ignore
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT content FROM bot_help_texts WHERE bot_name=%s AND language=%s",
                    (bot_name, language),
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception:
        return None


def upsert_doc_inventory(doc_path: str, doc_type: str, size_bytes: int) -> bool:
    dsn = get_dsn()
    if not dsn:
        return False
    try:
        import psycopg2  # type: ignore
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO docs_inventory (doc_path, doc_type, last_generated_at, size_bytes)
                       VALUES (%s, %s, NOW(), %s)
                       ON CONFLICT (doc_path)
                       DO UPDATE SET doc_type = EXCLUDED.doc_type,
                                     last_generated_at = NOW(),
                                     size_bytes = EXCLUDED.size_bytes""",
                    (doc_path, doc_type, size_bytes),
                )
        return True
    except Exception:
        return False


if __name__ == "__main__":
    ok = apply_schema()
    print("schema applied" if ok else "schema apply FAILED (see stderr)")
