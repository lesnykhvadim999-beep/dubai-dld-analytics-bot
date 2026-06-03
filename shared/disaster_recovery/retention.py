"""Disaster Recovery — retention prune.

Политика (бакеты строятся в backup.py по календарю):
  • daily   → keep N последних (RETENTION_DAILY_DAYS, default 7)
  • weekly  → keep N (RETENTION_WEEKLY_WEEKS, default 12)
  • monthly → keep N (RETENTION_MONTHLY_MONTHS, default 12)
  • yearly  → keep forever

Идёт по storage backend (GitHub releases / R2 / B2 / local), сортирует assets
по mtime, удаляет лишние через backend.delete().

CLI:
    python -m shared.disaster_recovery.retention            # dry-run по умолчанию
    python -m shared.disaster_recovery.retention --apply    # реально удалить

Vadim Realty + RERA BRN 65011.
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

from . import _config as cfg
from .storage_adapter import get_backend

log = logging.getLogger("dr.retention")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

KEEP = {
    "daily":   cfg.RETENTION_DAILY_DAYS,
    "weekly":  cfg.RETENTION_WEEKLY_WEEKS,
    "monthly": cfg.RETENTION_MONTHLY_MONTHS,
    "yearly":  10_000,   # фактически forever
}


def _db_name_from_key(key: str) -> str:
    # bucket/<db>_YYYY-MM-DD_HH-MM.ext
    base = Path(key).name
    return base.split("_", 1)[0]


def prune(*, apply: bool = False) -> dict:
    backend = get_backend()
    summary: dict = {"backend": getattr(backend, "name", "?"),
                     "deleted": [], "kept": 0, "dry_run": not apply}

    for bucket, keep_n in KEEP.items():
        try:
            entries = backend.list(prefix=bucket)
        except Exception as e:
            log.warning("[dr.retention] list(%s) failed: %s", bucket, e)
            continue
        # group by db name → sort desc by mtime → keep top keep_n
        by_db: dict[str, list] = defaultdict(list)
        for key, size, mtime in entries:
            by_db[_db_name_from_key(key)].append((key, size, mtime))

        for db, items in by_db.items():
            items.sort(key=lambda x: x[2], reverse=True)
            survivors = items[:keep_n]
            victims = items[keep_n:]
            summary["kept"] += len(survivors)
            for key, size, mtime in victims:
                log.info("[dr.retention] %s %s → DELETE (%d B, mtime=%s)",
                         "WOULD" if not apply else "doing", key, size, mtime)
                if apply:
                    try:
                        backend.delete(key)
                        summary["deleted"].append(key)
                    except Exception as e:
                        log.warning("[dr.retention] delete %s failed: %s", key, e)
                else:
                    summary["deleted"].append(key)  # planned
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DR retention prune")
    ap.add_argument("--apply", action="store_true",
                    help="Actually delete (default: dry-run)")
    args = ap.parse_args(argv)

    res = prune(apply=args.apply)
    print(json.dumps(res, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
