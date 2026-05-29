"""mv_refresher.py — daily refresh of materialized views in analytics DB.

Currently refreshes:
  - mv_offplan_summary (off-plan area aggregates from dld_sales_unified) —
    used as Reelly API fallback if Reelly is down.

Usage:
    py mv_refresher.py            # one-shot refresh, prints status, exits
    py mv_refresher.py --loop     # loop forever, refresh once per 24h

Designed to be invoked by Railway cron (recommended: daily at 04:00 UTC, after
DLD ingest) OR by a long-running container with --loop.

DSN priority:
    ANALYTICS_DATABASE_URL → DATABASE_URL. Fail-fast if neither is set —
    we never silently fall back to a hard-coded production DSN.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import psycopg2

MV_REFRESH_LIST: list[tuple[str, bool]] = [
    # (matview_name, supports_concurrently)
    ("mv_offplan_summary", True),
]


def _dsn() -> str:
    dsn = os.environ.get("ANALYTICS_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("ANALYTICS_DATABASE_URL or DATABASE_URL required")
    return dsn


def refresh_once() -> dict:
    """Run REFRESH MATERIALIZED VIEW for each entry. Returns per-MV stats."""
    out: dict[str, dict] = {}
    conn = psycopg2.connect(_dsn(), connect_timeout=30)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for mv_name, concurrent in MV_REFRESH_LIST:
                cur.execute(
                    "SELECT 1 FROM pg_matviews WHERE matviewname=%s",
                    (mv_name,),
                )
                if not cur.fetchone():
                    out[mv_name] = {"status": "missing"}
                    continue

                t0 = time.time()
                sql = (
                    f"REFRESH MATERIALIZED VIEW CONCURRENTLY {mv_name}"
                    if concurrent
                    else f"REFRESH MATERIALIZED VIEW {mv_name}"
                )
                try:
                    cur.execute(sql)
                    dt = time.time() - t0
                    cur.execute(f"SELECT COUNT(*) FROM {mv_name}")
                    rows = cur.fetchone()[0]
                    out[mv_name] = {"status": "ok", "seconds": round(dt, 2), "rows": rows}
                except Exception as exc:
                    out[mv_name] = {"status": "error", "error": str(exc)}
    finally:
        conn.close()
    return out


def _log(line: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {line}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Loop forever, sleep 24h between runs")
    parser.add_argument("--interval", type=float, default=86400.0, help="Loop interval seconds")
    args = parser.parse_args()

    while True:
        try:
            stats = refresh_once()
            for name, info in stats.items():
                _log(f"{name}: {info}")
        except Exception as exc:
            _log(f"FATAL: {exc}")
            traceback.print_exc()
        if not args.loop:
            return 0
        _log(f"sleeping {args.interval:.0f}s until next refresh")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
