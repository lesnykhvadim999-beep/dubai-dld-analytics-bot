"""PHASE BO O2 — Unified observability module.

Public API:
  fetch_system_health()    — dict snapshot from v_system_health (intelligence DB)
  compute_health_score()   — int 0-100 (penalty per OPEN/CRITICAL)
  build_telegram_digest()  — formatted RU/EN digest with footer
  send_digest()            — fire-and-forget hourly digest via admin_notify

DSN: read from env only (INTELLIGENCE_DATABASE_URL -> DATABASE_URL).
Footer: 'Vadim Realty (RERA BRN 65011)'.
"""
from .digest import (
    fetch_system_health,
    compute_health_score,
    build_telegram_digest,
    send_digest,
)

__all__ = [
    "fetch_system_health",
    "compute_health_score",
    "build_telegram_digest",
    "send_digest",
]
