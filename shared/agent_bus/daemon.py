"""
agent_bus.daemon — runnable subscriber.

Two modes:
  python -m agent_bus.daemon --agent admin-notifier --once
       — single-pass for cron (Windows Task Scheduler / Railway cron).
  python -m agent_bus.daemon --agent admin-notifier --loop
       — long-lived LISTEN/NOTIFY daemon (Windows Service / Railway worker).

Built-in handler set focuses on the admin-notifier agent which dispatches
bot.error_occurred to Telegram admin chat. Other agents are expected to
import run_subscriber_loop / run_subscriber_once and supply own dispatch.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict

# Bootstrap shared on path
for _p in ("/app/shared", r"C:\Projects\shared", "../shared"):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from agent_bus.registry import register_agent
from agent_bus.subscriber import run_subscriber_loop, run_subscriber_once

log = logging.getLogger("agent_bus.daemon")


# ── built-in handlers for admin-notifier agent ─────────────────────────
def _send_admin_alert(text: str) -> None:
    token = os.environ.get("ADMIN_BOT_TOKEN") or os.environ.get("HUB_BOT_TOKEN")
    chat = os.environ.get("ADMIN_CHAT_ID") or os.environ.get("VADIM_CHAT_ID")
    if not (token and chat):
        log.info("admin alert (no creds): %s", text[:200])
        return
    try:
        import urllib.parse
        import urllib.request
        body = urllib.parse.urlencode({
            "chat_id": chat,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        urllib.request.urlopen(url, data=body, timeout=10).read()
    except Exception as e:
        log.warning("admin alert send failed: %s", e)


def _h_bot_error(payload: Dict[str, Any], event: Dict[str, Any]) -> None:
    bot = event.get("source_bot", "?")
    err = payload.get("error", "")
    where = payload.get("where", "")
    msg = (
        f"<b>[{bot}] error</b>\n"
        f"<i>{where}</i>\n"
        f"<code>{str(err)[:1500]}</code>"
    )
    _send_admin_alert(msg)


def _h_market_shift(payload: Dict[str, Any], event: Dict[str, Any]) -> None:
    area = payload.get("area", "?")
    delta = payload.get("delta_pct", "?")
    _send_admin_alert(
        f"<b>market.shift</b> area={area} delta={delta}%\n"
        f"src={event.get('source_bot')}"
    )


def _h_listing_low(payload: Dict[str, Any], event: Dict[str, Any]) -> None:
    area = payload.get("area", "?")
    price = payload.get("price", "?")
    bench = payload.get("benchmark", "?")
    log.info("listing.priced_low area=%s price=%s bench=%s", area, price, bench)


_DEFAULT_DISPATCH = {
    "bot.error_occurred":      _h_bot_error,
    "market.shift_detected":   _h_market_shift,
    "listing.priced_low":      _h_listing_low,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="admin-notifier", help="agent_registry name")
    ap.add_argument("--once", action="store_true", help="single-pass (cron mode)")
    ap.add_argument("--loop", action="store_true", help="long-lived LISTEN mode")
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--subscribes", default="",
                    help="comma-separated event types (defaults to built-in set)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    if args.subscribes:
        subs = [s.strip() for s in args.subscribes.split(",") if s.strip()]
        dispatch = {s: _DEFAULT_DISPATCH.get(s, _h_bot_error) for s in subs}
    else:
        dispatch = dict(_DEFAULT_DISPATCH)

    register_agent(
        agent_name=args.agent,
        subscribes_to=list(dispatch.keys()),
        handler_endpoint="local:agent_bus.daemon",
    )

    if args.loop:
        log.info("agent_bus daemon LOOP mode agent=%s subs=%s",
                 args.agent, list(dispatch.keys()))
        run_subscriber_loop(args.agent, dispatch)
        return 0

    n = run_subscriber_once(args.agent, dispatch, batch=args.batch)
    log.info("agent_bus daemon ONCE processed=%s agent=%s", n, args.agent)
    print(json.dumps({"processed": n, "agent": args.agent}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
