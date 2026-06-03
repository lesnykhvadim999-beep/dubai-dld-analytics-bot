"""Outer watchdog — runs as a separate process on the host (or Railway service).

Reads heartbeat files written by each bot's internal watchdog
(/tmp/<bot>.alive) and alerts @vadim_admin_bot if any bot's heartbeat
is stale > BOT_WATCHDOG_STALE_SEC (default 120 sec).

Env:
  ADMIN_BOT_TOKEN, ADMIN_CHAT_ID    — Telegram alert target
  BOT_WATCHDOG_INTERVAL  (default 60)
  BOT_WATCHDOG_STALE_SEC (default 120)
  BOT_WATCHDOG_BOTS      (default "resale,hub,channel,lead,roi,analytics,currency")
  BOT_WATCHDOG_HEARTBEAT_DIR (default /tmp on POSIX, %TEMP% on Windows)

Notify rate-limit: a bot that is stale will be reported only once every
BOT_WATCHDOG_NOTIFY_INTERVAL sec (default 900 = 15 min) so we don't spam.

Run: `python bot_watchdog.py` (long-running).
"""
from __future__ import annotations
import os
import sys
import time
import json
import tempfile
import urllib.request
import urllib.parse


DEFAULT_BOTS = ("resale", "hub", "channel", "lead", "roi", "analytics", "currency")


def _heartbeat_dir() -> str:
    explicit = os.environ.get("BOT_WATCHDOG_HEARTBEAT_DIR", "").strip()
    if explicit:
        return explicit
    # /tmp exists on POSIX/Railway; on Windows fall back to %TEMP%
    if os.path.isdir("/tmp"):
        return "/tmp"
    return tempfile.gettempdir()


def _heartbeat_path(bot: str) -> str:
    return os.path.join(_heartbeat_dir(), f"{bot}.alive")


def _read_heartbeat(bot: str) -> float | None:
    p = _heartbeat_path(bot)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            txt = fh.read().strip()
        return float(txt)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None


def _notify(text: str) -> bool:
    token = os.environ.get("ADMIN_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("ADMIN_CHAT_ID", "353806371").strip()
    if not token:
        print("[bot_watchdog] ADMIN_BOT_TOKEN not set — cannot notify", flush=True)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text[:4090],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[bot_watchdog] notify failed: {e}", flush=True)
        return False


def main() -> int:
    interval = int(os.environ.get("BOT_WATCHDOG_INTERVAL", "60"))
    stale_sec = int(os.environ.get("BOT_WATCHDOG_STALE_SEC", "120"))
    notify_interval = int(os.environ.get("BOT_WATCHDOG_NOTIFY_INTERVAL", "900"))
    bots_env = os.environ.get("BOT_WATCHDOG_BOTS", "").strip()
    bots = [b.strip() for b in bots_env.split(",") if b.strip()] or list(DEFAULT_BOTS)

    print(f"[bot_watchdog] starting — bots={bots} interval={interval}s "
          f"stale={stale_sec}s notify_cooldown={notify_interval}s", flush=True)

    last_notified: dict[str, float] = {}

    while True:
        now = time.time()
        for bot in bots:
            ts = _read_heartbeat(bot)
            if ts is None:
                # No heartbeat file yet — bot hasn't started writing or doesn't run
                # on this host. Skip silently.
                continue
            age = now - ts
            if age > stale_sec:
                last = last_notified.get(bot, 0.0)
                if now - last >= notify_interval:
                    mins = int(age // 60)
                    secs = int(age % 60)
                    _notify(
                        f"⚠️ <b>Bot heartbeat stale</b>\n"
                        f"bot: <code>{bot}</code>\n"
                        f"last heartbeat: {mins}m{secs}s ago\n"
                        f"file: <code>{_heartbeat_path(bot)}</code>"
                    )
                    last_notified[bot] = now
                    print(f"[bot_watchdog] STALE bot={bot} age={age:.0f}s — notified", flush=True)
                else:
                    print(f"[bot_watchdog] STALE bot={bot} age={age:.0f}s — cooldown", flush=True)
            else:
                # healthy — clear cooldown so a future incident notifies right away
                if bot in last_notified:
                    print(f"[bot_watchdog] RECOVERED bot={bot} age={age:.0f}s", flush=True)
                    last_notified.pop(bot, None)
        time.sleep(interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("[bot_watchdog] interrupted", flush=True)
        sys.exit(0)
