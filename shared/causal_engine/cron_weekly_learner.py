"""Weekly cron entry for the causal learner.

Schedule (Railway cron or external):  Sunday 06:00 UTC

    0 6 * * 0  python -m shared.causal_engine.cron_weekly_learner

Also runs validator backtest after learning so confidence drifts toward
observed hit-rate.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback


def _admin_notify(msg: str) -> None:
    """Best-effort Telegram alert to admin (uses ADMIN_BOT_TOKEN / ADMIN_CHAT_ID env)."""
    token = os.getenv("ADMIN_BOT_TOKEN") or os.getenv("BOT_TOKEN")
    chat  = os.getenv("ADMIN_CHAT_ID")
    if not (token and chat):
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": msg[:3500], "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass


def main() -> int:
    started = time.time()
    summary = {"learner": None, "validator": None, "errors": []}
    try:
        sys.path.insert(0, r"C:\Projects")
        from shared.causal_engine import learner, validator  # type: ignore

        try:
            summary["learner"] = learner.run_weekly()
        except Exception as e:
            summary["errors"].append(f"learner: {e}")
            traceback.print_exc()

        try:
            summary["validator"] = validator.backtest()
        except Exception as e:
            summary["errors"].append(f"validator: {e}")
            traceback.print_exc()

    except Exception as e:
        summary["errors"].append(f"bootstrap: {e}")
        traceback.print_exc()

    elapsed = round(time.time() - started, 1)
    print(f"[cron_weekly_learner] done in {elapsed}s :: {json.dumps(summary, ensure_ascii=False)}")

    _admin_notify(
        "🧠 *Causal Engine — weekly run*\n"
        f"`{elapsed}s`\n"
        f"learner: `{summary['learner']}`\n"
        f"validator: `{summary['validator']}`\n"
        f"errors: `{summary['errors']}`"
    )
    return 0 if not summary["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
