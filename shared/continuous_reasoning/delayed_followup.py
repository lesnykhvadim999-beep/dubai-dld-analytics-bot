"""
Layer 22 — Delayed follow-up dispatcher.

Cron каждые 5 минут берёт continuous_thoughts WHERE status='queued' AND fire_at<=NOW(),
проверяет rate-limit + opt-out, отправляет через Bot API соответствующего бота.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import requests

from ._db import due_thoughts, set_status
from .rate_limiter import can_send_followup

log = logging.getLogger("continuous_reasoning.dispatcher")

# Token resolution: env per-bot, fallback на shared MAP env.
BOT_TOKEN_MAP = {
    "resale-bot":      os.getenv("RESALE_BOT_TOKEN", ""),
    "lead-bot":        os.getenv("LEAD_BOT_TOKEN", ""),
    "channel-bot-new": os.getenv("CHANNEL_BOT_TOKEN", ""),
    "currency-bot":    os.getenv("CURRENCY_BOT_TOKEN", ""),
    "roi-bot":         os.getenv("ROI_BOT_TOKEN", ""),
    "hub-bot":         os.getenv("HUB_BOT_TOKEN", ""),
    "admin-bot":       os.getenv("ADMIN_BOT_TOKEN", ""),
}


def _send(bot: str, user_id: int, text: str) -> bool:
    token = BOT_TOKEN_MAP.get(bot, "")
    if not token:
        log.warning("no token for bot=%s — skip", bot)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": user_id, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        if r.status_code != 200:
            log.warning("send failed bot=%s code=%s body=%s",
                        bot, r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:  # noqa: BLE001
        log.exception("send error: %s", e)
        return False


def dispatch_due_followups(limit: int = 50) -> Dict[str, Any]:
    items = due_thoughts(limit)
    stats = {"due": len(items), "sent": 0, "skipped": 0, "opted_out": 0}
    for t in items:
        if not can_send_followup(t["user_id"]):
            set_status(t["id"], "skipped", skip_reason="rate_limited_or_opted_out")
            stats["skipped"] += 1
            continue
        ok = _send(t["bot"], t["user_id"], t["followup_msg"])
        if ok:
            set_status(t["id"], "sent")
            stats["sent"] += 1
        else:
            set_status(t["id"], "skipped", skip_reason="telegram_send_failed")
            stats["skipped"] += 1
    log.info("dispatch stats=%s", stats)
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(dispatch_due_followups())
