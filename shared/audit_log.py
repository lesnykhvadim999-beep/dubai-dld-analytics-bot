"""
Audit log — пишет admin-actions в таблицу admin_actions (resale DB).

Таблица:
    id BIGSERIAL PK
    user_id BIGINT
    action VARCHAR(50)
    target VARCHAR(100)
    payload JSONB
    timestamp TIMESTAMPTZ DEFAULT NOW()

Использование:
    from audit_log import audit_log
    audit_log(uid, action="approve_listing", target="123",
              payload={"reason": "manual"})

Никогда не бросает — fire-and-forget.
"""
from __future__ import annotations

import os
import json
import logging
from typing import Any, Optional

import psycopg2

log = logging.getLogger("audit_log")

RESALE_DB = os.environ.get("RESALE_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
if not RESALE_DB:
    raise RuntimeError("RESALE_DATABASE_URL or DATABASE_URL must be set")


def audit_log(
    user_id: Optional[int],
    action: str,
    target: Optional[str] = None,
    payload: Optional[dict] = None,
) -> bool:
    """Сохранить запись о действии. True = вставлено, False = ошибка (молча)."""
    try:
        with psycopg2.connect(RESALE_DB, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO admin_actions (user_id, action, target, payload)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        int(user_id) if user_id is not None else None,
                        (action or "")[:50],
                        (target or "")[:100] if target else None,
                        json.dumps(payload) if payload else None,
                    ),
                )
            conn.commit()
        return True
    except Exception as e:
        log.warning("audit_log failed: %s", e)
        return False


def recent_actions(limit: int = 50, user_id: Optional[int] = None) -> list[dict]:
    """Вернуть последние записи (для admin /audit-команды)."""
    try:
        with psycopg2.connect(RESALE_DB, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                if user_id is not None:
                    cur.execute(
                        """
                        SELECT id, user_id, action, target, payload, timestamp
                          FROM admin_actions WHERE user_id = %s
                         ORDER BY id DESC LIMIT %s
                        """,
                        (user_id, limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, user_id, action, target, payload, timestamp
                          FROM admin_actions ORDER BY id DESC LIMIT %s
                        """,
                        (limit,),
                    )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        log.warning("recent_actions failed: %s", e)
        return []
