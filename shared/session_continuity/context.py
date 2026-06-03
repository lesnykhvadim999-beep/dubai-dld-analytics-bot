"""
session_continuity.context — read/write cross-bot user context.

Storage: cross_bot_sessions table (JSONB current_context).
All writes are idempotent UPSERTs by telegram_id.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from ._db import connect, is_available

log = logging.getLogger("session_continuity.context")


def get_user_context(telegram_id: int) -> Dict[str, Any]:
    """Return current_context dict (empty if no row)."""
    if not is_available():
        return {}
    try:
        conn = connect()
        if conn is None:
            return {}
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT current_context, last_bot, last_intent, last_message_at "
                "FROM cross_bot_sessions WHERE telegram_id=%s",
                (telegram_id,),
            )
            r = cur.fetchone()
        conn.close()
        if not r:
            return {}
        ctx = r[0] or {}
        ctx["_last_bot"] = r[1]
        ctx["_last_intent"] = r[2]
        ctx["_last_message_at"] = r[3].isoformat() if r[3] else None
        return ctx
    except Exception as e:
        log.warning("get_user_context failed tg=%s: %s", telegram_id, e)
        return {}


def update_user_context(
    telegram_id: int,
    key: str,
    value: Any,
    bot: Optional[str] = None,
    intent: Optional[str] = None,
) -> bool:
    """Merge {key:value} into current_context; optionally update last_bot/intent."""
    if not is_available():
        return False
    try:
        conn = connect()
        if conn is None:
            return False
        patch = {key: value}
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cross_bot_sessions (telegram_id, current_context, last_bot, last_intent, last_message_at)
                VALUES (%s, %s::jsonb, %s, %s, NOW())
                ON CONFLICT (telegram_id) DO UPDATE SET
                    current_context = cross_bot_sessions.current_context || EXCLUDED.current_context,
                    last_bot        = COALESCE(EXCLUDED.last_bot, cross_bot_sessions.last_bot),
                    last_intent     = COALESCE(EXCLUDED.last_intent, cross_bot_sessions.last_intent),
                    last_message_at = NOW()
                """,
                (telegram_id, json.dumps(patch, default=str), bot, intent),
            )
        conn.close()
        return True
    except Exception as e:
        log.warning("update_user_context failed tg=%s: %s", telegram_id, e)
        return False


def touch_session(telegram_id: int, bot: str, intent: Optional[str] = None) -> bool:
    """Bump last_message_at + last_bot without changing context."""
    if not is_available():
        return False
    try:
        conn = connect()
        if conn is None:
            return False
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cross_bot_sessions (telegram_id, last_bot, last_intent, last_message_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (telegram_id) DO UPDATE SET
                    last_bot        = EXCLUDED.last_bot,
                    last_intent     = COALESCE(EXCLUDED.last_intent, cross_bot_sessions.last_intent),
                    last_message_at = NOW()
                """,
                (telegram_id, bot, intent),
            )
        conn.close()
        return True
    except Exception:
        return False
