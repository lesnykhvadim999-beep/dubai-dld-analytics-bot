"""
shared.proactive_agent.notifier — Telegram push with rate limiting + audit.

Rate limits (per user, per bot):
  - default        : max 3 notifications / 24h
  - reduced state  : max 1 notification / 72h
  - reengagement   : max 1 / 30 days (independent of above)

All sends are logged to proactive_notifications. If TELEGRAM_BOT_TOKEN env
for the given bot is not configured, the notification is logged with
delivery_status='blocked' (reason=no_token) — pipeline stays observable.
"""
from __future__ import annotations
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import urllib.request
import urllib.parse

from ._db import get_conn
from .opt_out import is_opted_out, is_reduced

# Map bot_name → env var holding its TG token
BOT_TOKEN_ENV = {
    "resale-bot": ("RESALE_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
    "dubai-dld-analytics-bot": ("ANALYTICS_BOT_TOKEN", "DLD_BOT_TOKEN",
                                 "TELEGRAM_BOT_TOKEN"),
    "dubai-dld-analytics-bot-main": ("ANALYTICS_BOT_TOKEN", "DLD_BOT_TOKEN",
                                      "TELEGRAM_BOT_TOKEN"),
    "hub-bot": ("HUB_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
    "roi-bot": ("ROI_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
    "channel-bot-new": ("CHANNEL_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
    "lead-bot": ("LEAD_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
}

OPT_OUT_CALLBACK_PREFIX = "pa_optout_"


def _resolve_token(bot_name: str) -> Optional[str]:
    for env_key in BOT_TOKEN_ENV.get(bot_name, ("TELEGRAM_BOT_TOKEN",)):
        v = os.environ.get(env_key)
        if v:
            return v
    return None


def _rate_check(user_id: int, bot_name: str,
                trigger_type: Optional[str]) -> Tuple[bool, str]:
    """Return (allowed, reason_if_blocked)."""
    if is_opted_out(user_id, bot_name, trigger_type):
        return False, "opted_out"
    conn = get_conn()
    if not conn:
        return True, ""  # fail-open
    try:
        with conn.cursor() as cur:
            # default 24h cap
            cap_24h = 1 if is_reduced(user_id, bot_name) else 3
            cur.execute("""
                SELECT COUNT(*) FROM proactive_notifications
                WHERE user_id = %s AND bot_name = %s
                  AND sent_ts > NOW() - INTERVAL '24 hours'
                  AND delivery_status = 'sent'
            """, (user_id, bot_name))
            cnt_24h = cur.fetchone()[0]
            if cnt_24h >= cap_24h:
                return False, f"rate_limit_24h({cnt_24h}/{cap_24h})"
            # reengagement → 1 / 30 days
            if trigger_type == "reengagement":
                cur.execute("""
                    SELECT COUNT(*) FROM proactive_notifications
                    WHERE user_id = %s AND bot_name = %s
                      AND trigger_type = 'reengagement'
                      AND sent_ts > NOW() - INTERVAL '30 days'
                      AND delivery_status = 'sent'
                """, (user_id, bot_name))
                if cur.fetchone()[0] >= 1:
                    return False, "reengagement_30d_cap"
        return True, ""
    except Exception:
        return True, ""


def _build_opt_out_keyboard(trigger_type: Optional[str],
                            language: str = "ru") -> Dict[str, Any]:
    if language.startswith("ru"):
        less = "Меньше уведомлений"
        stop = "Отключить уведомления"
    else:
        less = "Fewer notifications"
        stop = "Turn off notifications"
    buttons = [
        [{"text": less,
          "callback_data": f"{OPT_OUT_CALLBACK_PREFIX}less"}],
        [{"text": stop,
          "callback_data": f"{OPT_OUT_CALLBACK_PREFIX}stop"}],
    ]
    if trigger_type:
        buttons.insert(0, [{"text": "Не присылать такое",
                            "callback_data": f"{OPT_OUT_CALLBACK_PREFIX}mute:{trigger_type}"}])
    return {"inline_keyboard": buttons}


def _tg_send(token: str, chat_id: int, text: str,
             reply_markup: Optional[Dict[str, Any]] = None,
             timeout: int = 12) -> Tuple[bool, Optional[str]]:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            ok = r.status == 200 and '"ok":true' in body
            return ok, None if ok else body[:300]
    except Exception as e:
        return False, str(e)[:300]


def _log_notification(user_id: int, bot_name: str, trigger_id: Optional[int],
                      trigger_type: Optional[str], payload: Dict[str, Any],
                      preview: str, status: str,
                      error: Optional[str] = None) -> None:
    conn = get_conn()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO proactive_notifications
                  (user_id, bot_name, trigger_id, trigger_type, payload,
                   message_preview, delivery_status, error)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            """, (user_id, bot_name, trigger_id, trigger_type,
                  json.dumps(payload), preview[:300], status, error))
    except Exception:
        pass
    # mirror into bot_interactions (BL phase)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_interactions
                  (bot_name, user_id, query_type, query_params,
                   outcome, bot_response_preview, meta)
                VALUES (%s, %s, 'proactive_push', %s::jsonb, %s, %s, %s::jsonb)
            """, (bot_name, user_id,
                  json.dumps({"trigger_type": trigger_type}),
                  "success" if status == "sent" else "blocked",
                  preview[:300],
                  json.dumps({"layer": "BM_L10",
                              "trigger_id": trigger_id,
                              "error": error})))
    except Exception:
        pass


def send_notification(user_id: int, bot_name: str, text: str,
                      trigger_type: Optional[str] = None,
                      trigger_id: Optional[int] = None,
                      payload: Optional[Dict[str, Any]] = None,
                      language: str = "ru",
                      include_opt_out: bool = True,
                      dry_run: bool = False) -> Dict[str, Any]:
    """Send a proactive push to a user. Returns result dict.

    Always rate-limited + opt-out aware. Never raises.
    """
    result = {"sent": False, "status": "blocked", "error": None}
    payload = payload or {}

    allowed, reason = _rate_check(user_id, bot_name, trigger_type)
    if not allowed:
        result["status"] = "blocked"
        result["error"] = reason
        _log_notification(user_id, bot_name, trigger_id, trigger_type,
                          payload, text, "blocked", reason)
        return result

    if dry_run or os.environ.get("PROACTIVE_DRY_RUN") == "1":
        result["status"] = "dry_run"
        _log_notification(user_id, bot_name, trigger_id, trigger_type,
                          payload, text, "dry_run", None)
        return result

    token = _resolve_token(bot_name)
    if not token:
        result["error"] = "no_token"
        _log_notification(user_id, bot_name, trigger_id, trigger_type,
                          payload, text, "blocked", "no_token")
        return result

    kb = _build_opt_out_keyboard(trigger_type, language) if include_opt_out else None
    ok, err = _tg_send(token, user_id, text, reply_markup=kb)
    status = "sent" if ok else "failed"
    if not ok and err and "bot was blocked" in err.lower():
        status = "blocked"
    result["sent"] = ok
    result["status"] = status
    result["error"] = err
    _log_notification(user_id, bot_name, trigger_id, trigger_type,
                      payload, text, status, err)
    return result
