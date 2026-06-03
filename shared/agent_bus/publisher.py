"""
agent_bus.publisher — INSERT into agent_events + pg_notify.

Resilient: never raises out. If DB unavailable, logs and returns False.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, Optional

from ._db import connect, is_available

log = logging.getLogger("agent_bus.publisher")

_CURRENT_BOT: Optional[str] = None
_LOCK = threading.Lock()


def start_event_publishing(bot_name: str) -> None:
    """Call once on bot startup. Stores bot_name as default source_bot."""
    global _CURRENT_BOT
    with _LOCK:
        _CURRENT_BOT = bot_name
    log.info("agent_bus: publisher initialised for bot=%s", bot_name)


def publish_event(
    event_type: str,
    payload: Optional[Dict[str, Any]] = None,
    source_bot: Optional[str] = None,
) -> bool:
    """
    Insert event row. Trigger handles pg_notify on channel 'agent_events'.
    Returns True on success.
    """
    if not is_available():
        return False
    src = source_bot or _CURRENT_BOT or os.environ.get("BOT_NAME") or "unknown"
    payload = payload or {}
    try:
        conn = connect(autocommit=True)
        if conn is None:
            return False
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_events (source_bot, event_type, payload)
                VALUES (%s, %s, %s::jsonb)
                """,
                (src, event_type, json.dumps(payload, ensure_ascii=False, default=str)),
            )
        try:
            conn.close()
        except Exception:
            pass
        return True
    except Exception as e:
        log.warning("publish_event failed type=%s err=%s", event_type, e)
        return False


# ─── well-known event types ────────────────────────────────────────────
class Events:
    LEAD_CREATED      = "user.lead_created"
    LISTING_PRICED_LOW = "listing.priced_low"
    MARKET_SHIFT      = "market.shift_detected"
    BOT_ERROR         = "bot.error_occurred"
    USER_HANDOFF      = "user.handoff_requested"
