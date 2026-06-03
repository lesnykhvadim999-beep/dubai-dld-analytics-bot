"""
PHASE BM Layer 19 — Auto-Generated Content Pipeline.

Daily/weekly/monthly контент для channel-bot-new, approval queue в admin-bot.
Vadim Realty (RERA BRN 65011). Только бесплатные API.
"""
from .daily_market_post import generate_daily_market_post
from .weekly_top_deals import generate_weekly_top_deals
from .monthly_report import generate_monthly_report
from ._queue import enqueue, approve, reject, list_pending, mark_published

__all__ = [
    "generate_daily_market_post", "generate_weekly_top_deals", "generate_monthly_report",
    "enqueue", "approve", "reject", "list_pending", "mark_published",
]
