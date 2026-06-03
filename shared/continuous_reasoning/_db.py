"""DB-обёртка для continuous_thoughts + continuous_optout."""
import os
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

log = logging.getLogger("continuous_reasoning.db")

DSN = (
    os.getenv("RAILWAY_PG_DSN")
    or os.getenv("INTELLIGENCE_DATABASE_URL")
    or os.getenv("DATABASE_URL")
    or ""
)


def conn():
    if not DSN:
        raise RuntimeError(
            "continuous_reasoning DB DSN missing — set RAILWAY_PG_DSN "
            "or INTELLIGENCE_DATABASE_URL/DATABASE_URL env var"
        )
    return psycopg2.connect(DSN, connect_timeout=8)


def insert_thought(
    *, user_id: int, bot: str, trigger_msg: str,
    thought: str, followup_msg: str, fire_at: datetime,
) -> Optional[int]:
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO continuous_thoughts
                    (user_id, bot, trigger_msg, thought, followup_msg, fire_at)
                VALUES (%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (user_id, bot, trigger_msg[:2000], thought[:4000],
                 followup_msg[:3000], fire_at),
            )
            return cur.fetchone()[0]
    except Exception as e:  # noqa: BLE001
        log.exception("insert_thought failed: %s", e)
        return None


def due_thoughts(limit: int = 50) -> List[Dict[str, Any]]:
    try:
        with conn() as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM continuous_thoughts "
                    "WHERE status='queued' AND fire_at <= NOW() "
                    "ORDER BY fire_at ASC LIMIT %s",
                    (limit,),
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        log.exception("due_thoughts failed: %s", e)
        return []


def set_status(thought_id: int, status: str, *, skip_reason: Optional[str] = None) -> bool:
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE continuous_thoughts SET status=%s, "
                "sent_at=CASE WHEN %s='sent' THEN NOW() ELSE sent_at END, "
                "skip_reason=%s "
                "WHERE id=%s",
                (status, status, skip_reason, thought_id),
            )
            return cur.rowcount > 0
    except Exception as e:  # noqa: BLE001
        log.exception("set_status failed: %s", e)
        return False


def last_followup_age_hours(user_id: int) -> Optional[float]:
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT EXTRACT(EPOCH FROM (NOW()-MAX(sent_at)))/3600 "
                "FROM continuous_thoughts "
                "WHERE user_id=%s AND status='sent'",
                (user_id,),
            )
            row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else None
    except Exception as e:  # noqa: BLE001
        log.warning("last_followup_age failed: %s", e)
        return None


def optout(user_id: int, bot: Optional[str] = None) -> bool:
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO continuous_optout (user_id, bot) VALUES (%s,%s) "
                "ON CONFLICT (user_id) DO NOTHING",
                (user_id, bot),
            )
            return True
    except Exception as e:  # noqa: BLE001
        log.exception("optout failed: %s", e)
        return False


def is_opted_out_db(user_id: int) -> bool:
    """Unified opt-out check: L22 continuous_optout OR L10 proactive_opt_out (blocked).

    /noreminders из L10 (proactive_agent) теперь глушит и L22 (continuous_thoughts).
    """
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute("SELECT 1 FROM continuous_optout WHERE user_id=%s", (user_id,))
            if cur.fetchone() is not None:
                return True
            # Cross-check L10 proactive_opt_out (any bot blocked == global mute)
            try:
                cur.execute(
                    "SELECT 1 FROM proactive_opt_out "
                    "WHERE user_id=%s AND blocked=TRUE LIMIT 1",
                    (user_id,),
                )
                if cur.fetchone() is not None:
                    return True
            except Exception:
                pass  # table may not exist in this env — fall back to L22-only
            return False
    except Exception as e:  # noqa: BLE001
        log.warning("is_opted_out failed: %s", e)
        return False
