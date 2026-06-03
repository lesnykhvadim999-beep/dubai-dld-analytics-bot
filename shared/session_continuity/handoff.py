"""
session_continuity.handoff — generate cross-bot handoff links and notify target.

Telegram supports start parameters (https://t.me/<bot>?start=<payload>).
We encode a short handoff token referencing context in DB; the target bot
reads cross_bot_sessions on /start <token> and restores context.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any, Dict, Optional

from .context import get_user_context, update_user_context
from .intent_classifier import BOTS

log = logging.getLogger("session_continuity.handoff")


def build_handoff_link(target_bot: str, token: str) -> str:
    """Return t.me deep link with start payload."""
    info = BOTS.get(target_bot)
    if not info:
        return ""
    return f"https://t.me/{info['username']}?start=ho_{token}"


def handoff_to_bot(
    telegram_id: int,
    target_bot: str,
    message: Optional[str] = None,
    extra_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Prepare handoff. Writes handoff token + extra_context into user's session.
    Returns {"link": ..., "token": ..., "target": ..., "label": ...}.

    The target bot, on /start ho_<token>, should:
        ctx = get_user_context(telegram_id)
        if ctx.get("handoff_token") == token: restore ctx["handoff_payload"]
    """
    if target_bot not in BOTS:
        return {"link": "", "token": "", "target": target_bot, "label": ""}
    token = secrets.token_urlsafe(12)
    payload = {
        "from_message": message or "",
        "extra": extra_context or {},
    }
    update_user_context(
        telegram_id,
        "handoff_token",
        token,
        bot=target_bot,
        intent="handoff",
    )
    update_user_context(telegram_id, "handoff_payload", payload)
    link = build_handoff_link(target_bot, token)
    return {
        "link": link,
        "token": token,
        "target": target_bot,
        "label": BOTS[target_bot]["label"],
    }


def consume_handoff(telegram_id: int, token: str) -> Optional[Dict[str, Any]]:
    """
    Called by target bot on /start ho_<token>. Returns handoff payload if valid.
    Clears the token after read (single-use).
    """
    ctx = get_user_context(telegram_id)
    if ctx.get("handoff_token") != token:
        return None
    payload = ctx.get("handoff_payload") or {}
    # Single-use: clear token
    update_user_context(telegram_id, "handoff_token", None)
    update_user_context(telegram_id, "handoff_payload", None)
    return payload
