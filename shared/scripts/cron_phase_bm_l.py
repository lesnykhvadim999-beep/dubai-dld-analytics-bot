"""
PHASE BM Agent L — cron entrypoints.

Запуски (Railway scheduler или Windows Task Scheduler):

  # ежедневно 08:00 UTC
  python -m shared.scripts.cron_phase_bm_l daily_content

  # понедельник 09:00
  python -m shared.scripts.cron_phase_bm_l weekly_top

  # 1-го числа 10:00
  python -m shared.scripts.cron_phase_bm_l monthly_report

  # ежедневно 02:00
  python -m shared.scripts.cron_phase_bm_l self_modify_scan

  # каждые 5 минут
  python -m shared.scripts.cron_phase_bm_l followups
"""
from __future__ import annotations

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
        print("usage: cron_phase_bm_l <daily_content|weekly_top|monthly_report|"
              "self_modify_scan|followups>")
        return 2
    cmd = argv[0]
    if cmd == "daily_content":
        from shared.content_pipeline.daily_market_post import generate_daily_market_post
        print({"queue_id": generate_daily_market_post()})
        return 0
    if cmd == "weekly_top":
        from shared.content_pipeline.weekly_top_deals import generate_weekly_top_deals
        print({"queue_id": generate_weekly_top_deals()})
        return 0
    if cmd == "monthly_report":
        from shared.content_pipeline.monthly_report import generate_monthly_report
        print({"queue_id": generate_monthly_report()})
        return 0
    if cmd == "self_modify_scan":
        from shared.self_modify.daily_scan import run_daily_scan
        print(run_daily_scan())
        return 0
    if cmd == "followups":
        from shared.continuous_reasoning.delayed_followup import dispatch_due_followups
        print(dispatch_due_followups())
        return 0
    print(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
