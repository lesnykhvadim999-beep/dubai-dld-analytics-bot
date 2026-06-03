"""shared/tz_utils.py — Asia/Dubai timezone helpers.

Why: DLD market data lives in Asia/Dubai (UTC+4). Using datetime.utcnow() for
"today/yesterday" cron logic systematically smears day boundaries by 4 hours —
a daily report built at 00:30 Dubai time misses 20:00–23:59 UTC of the prior
calendar day if it filters on UTC date. Always use now_dubai()/today_dubai()
for market-day boundaries, and keep now_utc() for serialization timestamps.
"""
from __future__ import annotations
from datetime import datetime, date
from zoneinfo import ZoneInfo

DUBAI = ZoneInfo("Asia/Dubai")
UTC = ZoneInfo("UTC")


def now_utc() -> datetime:
    """Aware UTC datetime — use for DB serialization timestamps."""
    return datetime.now(UTC)


def now_dubai() -> datetime:
    """Aware Asia/Dubai datetime — use for market-day boundaries."""
    return datetime.now(DUBAI)


def today_dubai() -> date:
    """Current calendar date in Asia/Dubai."""
    return now_dubai().date()


__all__ = ["DUBAI", "UTC", "now_utc", "now_dubai", "today_dubai"]
