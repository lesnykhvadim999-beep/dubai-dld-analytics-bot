"""
bot_admin_notifier — Telegram approval flow for bot_pattern_discoveries.

Uses ADMIN_BOT_TOKEN/ADMIN_CHAT_ID env. Silent skip if not configured.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Sequence

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


def _esc(s: str) -> str:
    return (str(s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def _build_message(d: dict) -> str:
    fix = d.get("suggested_fix_json") or {}
    ev = d.get("evidence_json") or []
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except Exception:
            ev = [ev]
    samples = ev[:3]
    sample_block = "\n".join(f"  • <i>{_esc(s[:120])}</i>" for s in samples) or "  —"

    return (
        f"🧠 <b>Bot Pattern Discovered</b>\n\n"
        f"<b>Bot:</b> {_esc(d.get('bot_name', '?'))}\n"
        f"<b>Issue:</b> {_esc(d.get('issue_type', '?'))}\n"
        f"<b>Affected users:</b> {d.get('affected_users', 0)}\n"
        f"<b>Severity:</b> {_esc(d.get('severity', '?'))}\n"
        f"<b>Confidence:</b> {d.get('confidence', 0):.2f} · llm={_esc(fix.get('_provider', '?'))}\n\n"
        f"<b>Suggested fix</b> (<code>{_esc(fix.get('kind', '?'))}</code>):\n"
        f"  category=<code>{_esc(fix.get('category') or '—')}</code>\n"
        f"  key=<code>{_esc(fix.get('key') or '—')}</code>\n"
        f"  value=<code>{_esc(json.dumps(fix.get('value'), ensure_ascii=False))[:200]}</code>\n"
        f"  notes: <i>{_esc(fix.get('notes', '—'))}</i>\n\n"
        f"<b>Evidence samples:</b>\n{sample_block}"
    )


def _build_keyboard(discovery_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve & Apply", "callback_data": f"bpm:approve:{discovery_id}"},
                {"text": "❌ Reject", "callback_data": f"bpm:reject:{discovery_id}"},
            ],
            [
                {"text": "👀 10 more samples", "callback_data": f"bpm:more:{discovery_id}:1"},
            ],
        ]
    }


def send_discovery(discovery_id: int, row: dict) -> int | None:
    if not (ADMIN_TOKEN and ADMIN_CHAT_ID):
        log.info("bot_admin_notifier: ADMIN_BOT_TOKEN/ADMIN_CHAT_ID not set — skip")
        return None
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{ADMIN_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_CHAT_ID,
                "text": _build_message(row),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": _build_keyboard(discovery_id),
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("result", {}).get("message_id")
    except Exception as exc:
        log.warning("send_discovery failed: %s", exc)
        return None


def send_summary(text: str) -> int | None:
    if not (ADMIN_TOKEN and ADMIN_CHAT_ID):
        return None
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{ADMIN_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        return r.json().get("result", {}).get("message_id")
    except Exception as exc:
        log.warning("send_summary failed: %s", exc)
        return None
