"""
bot_pattern_applier — auto-apply approved bot_pattern_discoveries.

Supported kinds:
  - add_alias / add_synonym → writes to KnowledgeGraph (Level 5) if available,
    otherwise to local `bot_aliases` table fallback.
  - cache_query → writes to `bot_cache_config` table (advisory; bot read on boot).
  - code_change / improve_response → records as TODO (no auto-push). Admin notified.

Idempotent: re-applying the same fingerprint is a no-op.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ._db import conn_cursor

log = logging.getLogger(__name__)


def _ensure_fallback_tables() -> None:
    with conn_cursor() as (_, cur):
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_aliases (
              id BIGSERIAL PRIMARY KEY,
              category TEXT NOT NULL,
              alias_key TEXT NOT NULL,
              canonical_values JSONB NOT NULL,
              source TEXT DEFAULT 'pattern_mining',
              created_at TIMESTAMPTZ DEFAULT NOW(),
              UNIQUE (category, alias_key)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_cache_config (
              id BIGSERIAL PRIMARY KEY,
              query_pattern TEXT NOT NULL,
              ttl_minutes INT DEFAULT 60,
              source TEXT DEFAULT 'pattern_mining',
              created_at TIMESTAMPTZ DEFAULT NOW(),
              UNIQUE (query_pattern)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_code_change_proposals (
              id BIGSERIAL PRIMARY KEY,
              discovery_id BIGINT,
              issue TEXT,
              suggested_fix JSONB,
              created_at TIMESTAMPTZ DEFAULT NOW(),
              addressed BOOLEAN DEFAULT false
            );
        """)


def _add_alias_via_kg(category: str, key: str, values: list) -> dict | None:
    """Try to write through Level 5 KnowledgeGraph API. Returns result dict or None."""
    try:
        from knowledge_graph import KG  # type: ignore
    except Exception:
        return None
    try:
        result = KG.add_alias(category=category, key=key, values=values, source="pattern_mining")  # type: ignore[attr-defined]
        return {"backend": "knowledge_graph", "result": result}
    except Exception as exc:
        log.warning("KG.add_alias failed: %s", exc)
        return None


def _add_alias_fallback(category: str, key: str, values: list) -> dict:
    _ensure_fallback_tables()
    with conn_cursor() as (_, cur):
        cur.execute("""
            INSERT INTO bot_aliases (category, alias_key, canonical_values)
            VALUES (%s, %s, %s::jsonb)
            ON CONFLICT (category, alias_key) DO UPDATE
              SET canonical_values = EXCLUDED.canonical_values
            RETURNING id
        """, (category, key.lower().strip(), json.dumps(values, ensure_ascii=False)))
        new_id = cur.fetchone()[0]
    return {"backend": "bot_aliases", "id": new_id}


def _apply_add_alias(fix: dict) -> dict:
    category = (fix.get("category") or "building").strip()
    key = (fix.get("key") or "").strip().lower()
    values = fix.get("value") or []
    if not key or not values:
        return {"status": "skipped", "reason": "missing key or value"}
    if isinstance(values, str):
        values = [values]
    # try KG, fallback to table
    via_kg = _add_alias_via_kg(category, key, values)
    if via_kg:
        return {"status": "applied", **via_kg}
    return {"status": "applied", **_add_alias_fallback(category, key, values)}


def _apply_cache_query(fix: dict) -> dict:
    _ensure_fallback_tables()
    pattern = (fix.get("key") or fix.get("query_pattern") or "").strip()
    ttl = int(fix.get("cache_ttl_minutes") or 60)
    if not pattern:
        return {"status": "skipped", "reason": "missing pattern"}
    with conn_cursor() as (_, cur):
        cur.execute("""
            INSERT INTO bot_cache_config (query_pattern, ttl_minutes)
            VALUES (%s, %s)
            ON CONFLICT (query_pattern) DO UPDATE SET ttl_minutes = EXCLUDED.ttl_minutes
            RETURNING id
        """, (pattern, ttl))
        new_id = cur.fetchone()[0]
    return {"status": "applied", "backend": "bot_cache_config", "id": new_id}


def _apply_code_change(discovery_id: int, fix: dict, issue: str) -> dict:
    _ensure_fallback_tables()
    with conn_cursor() as (_, cur):
        cur.execute("""
            INSERT INTO bot_code_change_proposals (discovery_id, issue, suggested_fix)
            VALUES (%s, %s, %s::jsonb)
            RETURNING id
        """, (discovery_id, issue, json.dumps(fix, ensure_ascii=False, default=str)))
        new_id = cur.fetchone()[0]
    return {"status": "queued", "backend": "bot_code_change_proposals", "id": new_id}


def apply_discovery(discovery_id: int) -> dict:
    """Apply an approved bot_pattern_discoveries row. Idempotent."""
    with conn_cursor(dict_cursor=True) as (_, cur):
        cur.execute(
            "SELECT * FROM bot_pattern_discoveries WHERE id = %s",
            (discovery_id,),
        )
        row = cur.fetchone()
    if not row:
        return {"status": "error", "reason": "discovery not found"}
    if row["status"] == "applied":
        return {"status": "noop", "reason": "already applied"}
    if row["status"] not in ("approved", "pending"):
        return {"status": "error", "reason": f"status={row['status']}"}

    fix = row.get("suggested_fix_json") or {}
    if isinstance(fix, str):
        try:
            fix = json.loads(fix)
        except Exception:
            return {"status": "error", "reason": "bad suggested_fix_json"}

    kind = (fix.get("kind") or "").lower()
    if kind in ("add_alias", "add_synonym"):
        result = _apply_add_alias(fix)
    elif kind == "cache_query":
        result = _apply_cache_query(fix)
    elif kind in ("code_change", "improve_response"):
        result = _apply_code_change(discovery_id, fix, row.get("issue_type", ""))
    else:
        result = {"status": "skipped", "reason": f"unsupported kind={kind}"}

    if result.get("status") in ("applied", "queued"):
        with conn_cursor() as (_, cur):
            cur.execute(
                """
                UPDATE bot_pattern_discoveries
                SET status = 'applied',
                    applied_at = NOW(),
                    applied_result = %s::jsonb
                WHERE id = %s
                """,
                (json.dumps(result, ensure_ascii=False, default=str), discovery_id),
            )
    return result
