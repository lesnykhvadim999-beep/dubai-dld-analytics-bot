"""
shared.proactive_agent.opt_out — user preference & rate-limit state.

Three levels:
  - reduced  : user pressed "Меньше уведомлений" → cap = 1/3 days
  - blocked  : full opt-out from proactive pushes
  - blocked_types: mute one trigger_type (e.g. "reengagement")
"""
from __future__ import annotations
import sys
from typing import Optional, Tuple

from ._db import get_conn


def _get_row(user_id: int, bot_name: str):
    conn = get_conn()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT reduced, blocked, blocked_types
                FROM proactive_opt_out
                WHERE user_id = %s AND bot_name = %s
            """, (user_id, bot_name))
            r = cur.fetchone()
        if not r:
            return {"reduced": False, "blocked": False, "blocked_types": []}
        return {"reduced": bool(r[0]), "blocked": bool(r[1]),
                "blocked_types": list(r[2] or [])}
    except Exception as e:
        sys.stderr.write(f"[opt_out] get err: {e}\n")
        return None


def is_opted_out(user_id: int, bot_name: str,
                 trigger_type: Optional[str] = None) -> bool:
    row = _get_row(user_id, bot_name)
    if not row:
        return False
    if row["blocked"]:
        return True
    if trigger_type and trigger_type in row["blocked_types"]:
        return True
    return False


def is_reduced(user_id: int, bot_name: str) -> bool:
    row = _get_row(user_id, bot_name)
    return bool(row and row["reduced"])


def reduce_notifications(user_id: int, bot_name: str) -> bool:
    conn = get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO proactive_opt_out (user_id, bot_name, reduced)
                VALUES (%s, %s, TRUE)
                ON CONFLICT (user_id, bot_name) DO UPDATE
                SET reduced = TRUE, updated_at = NOW()
            """, (user_id, bot_name))
        return True
    except Exception as e:
        sys.stderr.write(f"[opt_out] reduce err: {e}\n")
        return False


def block_notifications(user_id: int, bot_name: str,
                        trigger_type: Optional[str] = None) -> bool:
    conn = get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            if trigger_type:
                cur.execute("""
                    INSERT INTO proactive_opt_out
                      (user_id, bot_name, blocked_types)
                    VALUES (%s, %s, ARRAY[%s])
                    ON CONFLICT (user_id, bot_name) DO UPDATE
                    SET blocked_types = (
                        SELECT ARRAY(SELECT DISTINCT unnest(
                          COALESCE(proactive_opt_out.blocked_types, '{}')
                          || ARRAY[%s]))),
                        updated_at = NOW()
                """, (user_id, bot_name, trigger_type, trigger_type))
            else:
                cur.execute("""
                    INSERT INTO proactive_opt_out (user_id, bot_name, blocked)
                    VALUES (%s, %s, TRUE)
                    ON CONFLICT (user_id, bot_name) DO UPDATE
                    SET blocked = TRUE, updated_at = NOW()
                """, (user_id, bot_name))
        return True
    except Exception as e:
        sys.stderr.write(f"[opt_out] block err: {e}\n")
        return False


OPT_OUT_CALLBACK_PREFIX = "pa_optout_"


def handle_opt_out_callback(data: str, user_id: int,
                            bot_name: str) -> Tuple[bool, str]:
    """Decode a callback_data string from a notification button.

    Format: '{OPT_OUT_CALLBACK_PREFIX}{action}[:{trigger_type}]'
    actions: 'less' | 'stop' | 'mute'
    Returns (ok, human_reply).
    """
    if not data or not data.startswith(OPT_OUT_CALLBACK_PREFIX):
        return False, ""
    rest = data[len(OPT_OUT_CALLBACK_PREFIX):]
    parts = rest.split(":", 1)
    action = parts[0]
    ttype = parts[1] if len(parts) > 1 else None
    if action == "less":
        ok = reduce_notifications(user_id, bot_name)
        return ok, "Хорошо, буду писать реже." if ok else ""
    if action == "stop":
        ok = block_notifications(user_id, bot_name)
        return ok, "Уведомления отключены. Включить обратно — /notify_on" if ok else ""
    if action == "mute" and ttype:
        ok = block_notifications(user_id, bot_name, trigger_type=ttype)
        return ok, "Этот тип уведомлений отключён." if ok else ""
    return False, ""


def reenable_notifications(user_id: int, bot_name: str) -> bool:
    conn = get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE proactive_opt_out
                SET blocked = FALSE, reduced = FALSE, blocked_types = NULL,
                    updated_at = NOW()
                WHERE user_id = %s AND bot_name = %s
            """, (user_id, bot_name))
        return True
    except Exception:
        return False
