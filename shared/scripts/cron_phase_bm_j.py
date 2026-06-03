"""
PHASE BM Agent J — cron entrypoints (Layer 15 Meta-Learning + Layer 16 External Knowledge).

Schedules (Railway scheduler or Windows Task Scheduler):

  # every 3 hours
  python -m shared.scripts.cron_phase_bm_j rss_poll

  # every 2 hours
  python -m shared.scripts.cron_phase_bm_j telegram_scrape

  # weekly: Sunday 03:00
  python -m shared.scripts.cron_phase_bm_j meta_optimize

  # daily 08:00 UTC — channel-bot-new digest
  python -m shared.scripts.cron_phase_bm_j daily_digest
"""
from __future__ import annotations

import json
import logging
import sys


def _log():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main(argv=None) -> int:
    _log()
    argv = argv or sys.argv[1:]
    if not argv:
        print("usage: cron_phase_bm_j <rss_poll|telegram_scrape|meta_optimize|daily_digest>")
        return 2
    cmd = argv[0]

    if cmd == "rss_poll":
        from shared.external_knowledge.rss_poller import poll_all
        print(json.dumps(poll_all()))
        return 0

    if cmd == "telegram_scrape":
        from shared.external_knowledge.telegram_scraper import scrape
        print(json.dumps(scrape()))
        return 0

    if cmd == "meta_optimize":
        from shared.meta_learner.optimizer import recompute
        window = 14
        if len(argv) > 1:
            try:
                window = int(argv[1])
            except ValueError:
                pass
        print(json.dumps(recompute(window_days=window)))
        return 0

    if cmd == "daily_digest":
        # Post Top-5 news to channel-bot-new feed.
        from shared.external_knowledge import render_daily_digest_md
        md = render_daily_digest_md(top_k=5)
        if not md.strip():
            print(json.dumps({"posted": 0, "reason": "no signals"}))
            return 0
        import os
        try:
            import requests  # type: ignore
        except ImportError:
            print(json.dumps({"posted": 0, "reason": "requests missing"}))
            return 0
        token = os.environ.get("CHANNEL_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
        chat_id = (os.environ.get("DAILY_DIGEST_CHAT_ID")
                   or os.environ.get("CHANNEL_BOT_TARGET_CHAT_ID"))
        if not (token and chat_id):
            print(json.dumps({"posted": 0, "reason": "no token/chat", "preview_len": len(md)}))
            return 0
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": md,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": False,
                },
                timeout=20,
            )
            ok = r.ok and r.json().get("ok", False)
            print(json.dumps({"posted": 1 if ok else 0, "status": r.status_code}))
        except Exception as e:
            print(json.dumps({"posted": 0, "error": str(e)}))
        return 0

    print(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
