"""Universal admin notification utility — sends to @vadim_admin_bot.

Used by ALL services to push alerts, daily digests, watchdog events,
manual review prompts to a single Telegram chat.

Env:
  ADMIN_BOT_TOKEN — required (token of @vadim_admin_bot)
  ADMIN_CHAT_ID   — defaults to 353806371 (Vadim)
"""
from __future__ import annotations
import os
import json
import urllib.request
import urllib.parse
from typing import Optional

ADMIN_BOT_TOKEN_ENV = "ADMIN_BOT_TOKEN"
ADMIN_CHAT_ID_ENV = "ADMIN_CHAT_ID"
DEFAULT_CHAT_ID = "353806371"


def admin_notify(text: str,
                  parse_mode: str = "HTML",
                  disable_preview: bool = True,
                  buttons: Optional[list[list[dict]]] = None) -> bool:
    """Send a Telegram message to @vadim_admin_bot.

    Returns True if Telegram accepted the message, False otherwise (never
    raises — designed for fire-and-forget background callers).
    """
    token = os.environ.get(ADMIN_BOT_TOKEN_ENV, "").strip()
    if not token:
        print("[admin_notify] ADMIN_BOT_TOKEN not set")
        return False
    chat_id = os.environ.get(ADMIN_CHAT_ID_ENV, DEFAULT_CHAT_ID)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict = {
        "chat_id": chat_id,
        "text": text[:4090],
        "parse_mode": parse_mode,
        "disable_web_page_preview": "true" if disable_preview else "false",
    }
    if buttons:
        payload["reply_markup"] = json.dumps({
            "inline_keyboard": buttons,
        })
    try:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        print(f"[admin_notify] err: {e}")
        return False


if __name__ == "__main__":
    # smoke test
    import sys
    msg = sys.argv[1] if len(sys.argv) > 1 else "🔧 admin_notify test"
    ok = admin_notify(msg)
    print("sent:", ok)
