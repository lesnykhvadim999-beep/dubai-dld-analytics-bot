"""Idempotent boot hook: applies schema + seeds quota_limits.

Called on bot startup (Railway):
    python -m shared.quota_tracker.boot

Safe to call repeatedly — uses IF NOT EXISTS / UPSERT.
"""
from __future__ import annotations

import logging
import sys

from .limits import seed_limits

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("quota_tracker.boot")


def main() -> int:
    log.info("quota_tracker boot: applying schema + seeding quota_limits")
    result = seed_limits()
    ok = sum(1 for v in result.values() if v)
    total = len(result)
    log.info("quota_tracker boot: seeded %d/%d provider rows", ok, total)
    if ok == 0:
        log.warning("quota_tracker boot: nothing seeded — DSN missing or DB unreachable")
        return 0  # do not fail the bot boot
    return 0


if __name__ == "__main__":
    sys.exit(main())
