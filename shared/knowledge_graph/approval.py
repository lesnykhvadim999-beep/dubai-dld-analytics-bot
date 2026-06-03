"""Telegram approval workflow for KG entries.

Reuses ADMIN_BOT_TOKEN/ADMIN_CHAT_ID convention from parser_self_improve.admin_notifier.
Callback data scheme:
    kg:approve:<id>
    kg:reject:<id>
    kg:context:<id>
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

import requests

log = logging.getLogger(__name__)

ADMIN_TOKEN = (
    os.environ.get("ADMIN_BOT_TOKEN")
    or os.environ.get("VADIM_ADMIN_BOT_TOKEN")
    or os.environ.get("RESALE_BOT_TOKEN")
)
ADMIN_CHAT_ID = (
    os.environ.get("ADMIN_CHAT_ID")
    or os.environ.get("VADIM_ADMIN_CHAT_ID")
)


def _esc(s) -> str:
    return (str(s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def _build_message(knowledge_id: int, category: str, description: str,
                   payload: dict, source_bot: str) -> str:
    pay_lines = []
    for k, v in (payload or {}).items():
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v[:6])
        s = str(v)[:200]
        pay_lines.append(f"  • <b>{_esc(k)}</b>: {_esc(s)}")
    pay_block = "\n".join(pay_lines) if pay_lines else "  (empty)"
    return (
        f"🧠 <b>New knowledge proposed</b> (#{knowledge_id})\n\n"
        f"<b>Category:</b> {_esc(category)}\n"
        f"<b>Source:</b> {_esc(source_bot)}\n"
        f"<b>Description:</b>\n<i>{_esc(description[:600])}</i>\n\n"
        f"<b>Payload:</b>\n{pay_block}\n"
    )


def _keyboard(knowledge_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"kg:approve:{knowledge_id}"},
        {"text": "❌ Reject", "callback_data": f"kg:reject:{knowledge_id}"},
    ], [
        {"text": "👁 More context", "callback_data": f"kg:context:{knowledge_id}"},
    ]]}


def notify_admin_for_approval(
    knowledge_id: int,
    category: str,
    description: str,
    payload: Optional[dict] = None,
    source_bot: str = "?",
) -> Optional[int]:
    if not (ADMIN_TOKEN and ADMIN_CHAT_ID):
        log.info("KG approval: ADMIN_BOT_TOKEN/CHAT_ID not set — skip notify")
        return None
    text = _build_message(knowledge_id, category, description, payload or {}, source_bot)
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{ADMIN_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": _keyboard(knowledge_id),
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("result", {}).get("message_id")
    except Exception as exc:  # noqa: BLE001
        log.warning("KG admin notify failed: %s", exc)
        return None


def send_text(text: str) -> Optional[int]:
    if not (ADMIN_TOKEN and ADMIN_CHAT_ID):
        return None
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{ADMIN_TOKEN}/sendMessage",
            json={"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        return r.json().get("result", {}).get("message_id")
    except Exception:
        return None


def handle_callback(callback_data: str, actor: str = "vadim") -> str:
    """Parse `kg:<action>:<id>` and apply. Returns human result text."""
    from .client import KG
    try:
        prefix, action, sid = callback_data.split(":", 2)
    except ValueError:
        return "bad callback"
    if prefix != "kg":
        return "not kg"
    try:
        kid = int(sid)
    except ValueError:
        return "bad id"
    if action == "approve":
        return "✅ approved" if KG.approve(kid, actor=actor) else "approve failed"
    if action == "reject":
        return "❌ rejected" if KG.reject(kid, actor=actor) else "reject failed"
    if action == "context":
        return f"context for #{kid} — see DB"
    return f"unknown action: {action}"
