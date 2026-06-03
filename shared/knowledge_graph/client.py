"""KG client — public API surface."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Iterable, List, Optional

from ._db import conn_cursor, ensure_schema
from .embeddings import embed_text, to_pgvector_literal, EMBED_DIM

log = logging.getLogger(__name__)

# Hot-path safety: KG.search timeout
SEARCH_TIMEOUT_MS = int(os.environ.get("KG_SEARCH_TIMEOUT_MS", "100"))
CACHE_TTL_SEC = int(os.environ.get("KG_CACHE_TTL_SEC", "300"))

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()
_schema_ready = False


def _ensure_once() -> None:
    global _schema_ready
    if _schema_ready:
        return
    try:
        ensure_schema()
        _schema_ready = True
    except Exception as exc:  # noqa: BLE001
        log.warning("KG.ensure_schema failed: %s", exc)


def _cache_get(key: str):
    with _cache_lock:
        rec = _cache.get(key)
        if rec and time.time() - rec[0] < CACHE_TTL_SEC:
            return rec[1]
    return None


def _cache_put(key: str, value: Any) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), value)


class KG:
    """Cross-bot Knowledge Graph client (static methods, no state).

    Categories:
      - building_alias: {"key": "<lower>", "aliases": [...], "canonical": "..."}
      - area_alias:     {"key": "<lower>", "aliases": [...], "canonical": "..."}
      - edge_case:      free-form payload
      - workflow_fix:   {"symptom": "...", "fix": "..."}
      - parser_pattern: PSI rule payload
      - faq:            {"question": "...", "answer": "..."}
      - response_template: {"slug": "...", "text": "..."}
    """

    # ──────────────── write ────────────────

    @staticmethod
    def add(
        category: str,
        payload: dict,
        description: str,
        source_bot: str,
        meta: Optional[dict] = None,
        auto_approve: bool = False,
        notify: bool = True,
    ) -> Optional[int]:
        """Add knowledge entry. Returns inserted id (None on duplicate/failure)."""
        _ensure_once()
        try:
            emb = embed_text(description)
            status = "approved" if auto_approve else "pending"
            with conn_cursor() as (_, cur):
                cur.execute(
                    """
                    INSERT INTO knowledge_graph
                      (category, source_bot, payload, description, embedding, status, validated, meta)
                    VALUES (%s, %s, %s::jsonb, %s, %s::vector, %s, %s, %s::jsonb)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    (
                        category,
                        source_bot,
                        json.dumps(payload, ensure_ascii=False),
                        description,
                        to_pgvector_literal(emb),
                        status,
                        auto_approve,
                        json.dumps(meta or {}, ensure_ascii=False),
                    ),
                )
                row = cur.fetchone()
            if not row:
                return None
            knowledge_id = row[0]
            if notify and not auto_approve:
                try:
                    from .approval import notify_admin_for_approval
                    notify_admin_for_approval(knowledge_id, category, description, payload, source_bot)
                except Exception as exc:  # noqa: BLE001
                    log.warning("KG notify failed: %s", exc)
            # invalidate alias cache for category
            with _cache_lock:
                for k in list(_cache.keys()):
                    if k.startswith(f"alias::{category}::"):
                        _cache.pop(k, None)
            return knowledge_id
        except Exception as exc:  # noqa: BLE001
            log.warning("KG.add failed (%s): %s", category, exc)
            return None

    # ──────────────── search ────────────────

    @staticmethod
    def search(
        query: str,
        category: Optional[str] = None,
        k: int = 5,
        only_approved: bool = True,
        bot_name: Optional[str] = None,
        user_id: Optional[int] = None,
        log_usage: bool = True,
    ) -> List[dict]:
        """Semantic search via pgvector. Returns list of dicts; [] on timeout/failure."""
        _ensure_once()
        cache_key = f"search::{category or '*'}::{int(only_approved)}::{k}::{query.lower().strip()[:200]}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            emb = embed_text(query)
            with conn_cursor(dict_cursor=True) as (_, cur):
                cur.execute(f"SET LOCAL statement_timeout = {SEARCH_TIMEOUT_MS}")
                cur.execute(
                    """
                    SELECT id, category, payload, description,
                           (upvotes - downvotes) AS score,
                           status,
                           (embedding <-> %s::vector) AS distance
                    FROM knowledge_graph
                    WHERE (%s::text IS NULL OR category = %s)
                      AND (NOT %s OR status = 'approved')
                      AND embedding IS NOT NULL
                    ORDER BY embedding <-> %s::vector
                    LIMIT %s
                    """,
                    (
                        to_pgvector_literal(emb),
                        category, category,
                        only_approved,
                        to_pgvector_literal(emb),
                        k,
                    ),
                )
                rows = [dict(r) for r in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            log.warning("KG.search timeout/fail (%s): %s", category, exc)
            return []

        if log_usage and rows:
            try:
                with conn_cursor() as (_, cur):
                    args = [
                        (r["id"], bot_name, user_id, query[:200], None) for r in rows[:1]
                    ]
                    cur.executemany(
                        """INSERT INTO knowledge_usage_log
                           (knowledge_id, bot_name, user_id, context, successful)
                           VALUES (%s, %s, %s, %s, %s)""",
                        args,
                    )
                    cur.execute(
                        "UPDATE knowledge_graph SET usage_count = usage_count + 1 WHERE id = %s",
                        (rows[0]["id"],),
                    )
            except Exception:
                pass

        _cache_put(cache_key, rows)
        return rows

    # ──────────────── alias-specific helpers ────────────────

    @staticmethod
    def get_aliases(
        name: str,
        category: str = "building_alias",
        fallback: Optional[Iterable[str]] = None,
    ) -> List[str]:
        """Get alias candidates for a building/area name.

        Order: direct key lookup (cached) → fallback (hardcoded) → [name].
        Never throws; safe for hot path.
        """
        if not name:
            return list(fallback or [])
        key = name.lower().strip()
        ck = f"alias::{category}::{key}"
        cached = _cache_get(ck)
        if cached is not None:
            return cached

        result: List[str] = []
        try:
            with conn_cursor(dict_cursor=True) as (_, cur):
                cur.execute(f"SET LOCAL statement_timeout = {SEARCH_TIMEOUT_MS}")
                cur.execute(
                    """SELECT payload FROM knowledge_graph
                       WHERE category = %s
                         AND status = 'approved'
                         AND payload->>'key' = %s
                       LIMIT 1""",
                    (category, key),
                )
                row = cur.fetchone()
                if row:
                    payload = row["payload"]
                    aliases = payload.get("aliases") or []
                    canon = payload.get("canonical")
                    result = list(aliases)
                    if canon and canon not in result:
                        result.insert(0, canon)
        except Exception as exc:  # noqa: BLE001
            log.debug("KG.get_aliases miss/timeout for %s: %s", key, exc)

        if not result:
            result = list(fallback or [name])

        _cache_put(ck, result)
        return result

    # ──────────────── feedback ────────────────

    @staticmethod
    def upvote(knowledge_id: int) -> None:
        try:
            with conn_cursor() as (_, cur):
                cur.execute(
                    "UPDATE knowledge_graph SET upvotes = upvotes + 1, updated_at = NOW() WHERE id = %s",
                    (knowledge_id,),
                )
        except Exception:
            pass

    @staticmethod
    def downvote(knowledge_id: int) -> None:
        try:
            with conn_cursor() as (_, cur):
                cur.execute(
                    "UPDATE knowledge_graph SET downvotes = downvotes + 1, updated_at = NOW() WHERE id = %s",
                    (knowledge_id,),
                )
        except Exception:
            pass

    @staticmethod
    def approve(knowledge_id: int, actor: str = "vadim", notes: str = "") -> bool:
        try:
            with conn_cursor() as (_, cur):
                cur.execute(
                    """UPDATE knowledge_graph
                       SET status='approved', validated=true, updated_at=NOW()
                       WHERE id=%s""",
                    (knowledge_id,),
                )
                cur.execute(
                    """INSERT INTO knowledge_approval_log (knowledge_id, action, actor, notes)
                       VALUES (%s, 'approve', %s, %s)""",
                    (knowledge_id, actor, notes),
                )
            with _cache_lock:
                _cache.clear()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("KG.approve failed: %s", exc)
            return False

    @staticmethod
    def reject(knowledge_id: int, actor: str = "vadim", notes: str = "") -> bool:
        try:
            with conn_cursor() as (_, cur):
                cur.execute(
                    "UPDATE knowledge_graph SET status='rejected', updated_at=NOW() WHERE id=%s",
                    (knowledge_id,),
                )
                cur.execute(
                    """INSERT INTO knowledge_approval_log (knowledge_id, action, actor, notes)
                       VALUES (%s, 'reject', %s, %s)""",
                    (knowledge_id, actor, notes),
                )
            return True
        except Exception:
            return False

    # ──────────────── stats ────────────────

    @staticmethod
    def stats() -> dict:
        try:
            with conn_cursor(dict_cursor=True) as (_, cur):
                cur.execute(
                    """SELECT
                         COUNT(*) AS total,
                         COUNT(*) FILTER (WHERE status='approved') AS approved,
                         COUNT(*) FILTER (WHERE status='pending') AS pending,
                         COUNT(*) FILTER (WHERE status='rejected') AS rejected
                       FROM knowledge_graph"""
                )
                base = dict(cur.fetchone())
                cur.execute(
                    """SELECT category, COUNT(*) AS n
                       FROM knowledge_graph WHERE status='approved'
                       GROUP BY category ORDER BY n DESC LIMIT 8"""
                )
                base["by_category"] = [dict(r) for r in cur.fetchall()]
                cur.execute(
                    "SELECT COUNT(*) AS n FROM knowledge_usage_log WHERE used_at >= NOW() - INTERVAL '24 hours'"
                )
                base["used_24h"] = cur.fetchone()["n"]
                # validation rate = approved / (approved + rejected)
                ar = (base.get("approved") or 0) + (base.get("rejected") or 0)
                base["validation_rate"] = round(100.0 * (base.get("approved") or 0) / ar, 1) if ar else 0.0
                return base
        except Exception as exc:  # noqa: BLE001
            log.warning("KG.stats failed: %s", exc)
            return {"total": 0, "approved": 0, "pending": 0, "rejected": 0, "by_category": [], "used_24h": 0, "validation_rate": 0.0}
