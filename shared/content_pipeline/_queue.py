"""Approval-queue над таблицей content_pipeline_queue."""
import os
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

log = logging.getLogger("content_pipeline.queue")

DSN = os.getenv(
    "RAILWAY_PG_DSN",
    os.environ.get("LIVE_DATABASE_URL") or os.environ.get("DATABASE_URL") or os.environ.get("INTELLIGENCE_DATABASE_URL") or "",
)


def _conn():
    return psycopg2.connect(DSN, connect_timeout=8)


def enqueue(
    kind: str,
    target_chat: str,
    body_md: str,
    *,
    title: Optional[str] = None,
    media_paths: Optional[List[str]] = None,
    payload: Optional[Dict[str, Any]] = None,
    scheduled_at: Optional[datetime] = None,
) -> Optional[int]:
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO content_pipeline_queue
                    (kind, target_chat, title, body_md, media_paths, payload, scheduled_at)
                VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
                RETURNING id
                """,
                (
                    kind, target_chat, title, body_md,
                    json.dumps(media_paths or []),
                    json.dumps(payload or {}, ensure_ascii=False, default=str),
                    scheduled_at,
                ),
            )
            return cur.fetchone()[0]
    except Exception as e:  # noqa: BLE001
        log.exception("enqueue failed: %s", e)
        return None


def list_pending(limit: int = 20) -> List[Dict[str, Any]]:
    try:
        with _conn() as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id,kind,target_chat,title,body_md,media_paths,scheduled_at,created_at "
                    "FROM content_pipeline_queue WHERE status='pending' "
                    "ORDER BY created_at ASC LIMIT %s",
                    (limit,),
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        log.exception("list_pending failed: %s", e)
        return []


def approve(queue_id: int, admin_id: int, note: str = "") -> bool:
    return _update_status(queue_id, "approved", admin_id, note)


def reject(queue_id: int, admin_id: int, note: str = "") -> bool:
    return _update_status(queue_id, "rejected", admin_id, note)


def mark_published(queue_id: int) -> bool:
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE content_pipeline_queue SET status='published', "
                "published_at=NOW(), updated_at=NOW() WHERE id=%s",
                (queue_id,),
            )
        return True
    except Exception as e:  # noqa: BLE001
        log.exception("mark_published failed: %s", e)
        return False


def _update_status(qid: int, status: str, admin_id: int, note: str) -> bool:
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE content_pipeline_queue SET status=%s, review_admin=%s, "
                "review_note=%s, updated_at=NOW() WHERE id=%s",
                (status, admin_id, note, qid),
            )
            return cur.rowcount > 0
    except Exception as e:  # noqa: BLE001
        log.exception("_update_status failed: %s", e)
        return False
