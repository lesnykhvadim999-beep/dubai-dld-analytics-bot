"""Realtime Auto-Audit — runs every 15 min via Windows Task Scheduler.

Detects fast-moving regressions and pushes an instant alert to
@vadim_admin_bot (throttled at most once per hour per signature):

  R1. parser intake stall — <30 new listings in the last hour
  R2. any bot HTTP /health != 200
  R3. any DB > 95% full
  R4. full LLM-chain outage — all probed providers failed in this run
  R5. listings_staging pending > STAGING_BACKLOG_HARD (default 75000)

Always writes one heartbeat row (`cron.tick.audit_realtime`) so the daily
audit can see we ourselves are alive.
"""
from __future__ import annotations
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from auto_audit._common import (  # noqa: E402
    DSNS, BOTS, LLM_PROVIDERS,
    admin_notify, ensure_schema, record_metric,
    should_alert, q, http_probe, llm_probe,
)

DB_LIMIT_MB = int(os.environ.get("DB_VOLUME_LIMIT_MB", "5000"))
PARSER_STALL_THRESHOLD = int(os.environ.get("PARSER_STALL_THRESHOLD", "30"))
STAGING_HARD = int(os.environ.get("STAGING_BACKLOG_HARD", "75000"))


def check_parser_intake() -> list[str]:
    rows = q(DSNS["resale"], """
        SELECT COUNT(*) AS c FROM public.listings
        WHERE created_at > NOW() - INTERVAL '1 hour'
    """)
    if not rows or rows[0].get("_error"):
        return []
    c = int(rows[0].get("c") or 0)
    record_metric("parser.intake_1h", c)
    if c < PARSER_STALL_THRESHOLD and should_alert("parser_stall", 3600):
        return [f"🔴 Parser stall: only {c} new listings last hour (<{PARSER_STALL_THRESHOLD})"]
    return []


def check_bots() -> list[str]:
    alerts: list[str] = []
    for name, url in BOTS:
        status, latency, body = http_probe(url, timeout=8)
        record_metric(f"bot.health.{name}",
                       1 if status == 200 else 0,
                       {"status": status, "latency_ms": int(latency)})
        if status != 200:
            key = f"bot_{name}_{status}"
            if should_alert(key, 3600):
                alerts.append(f"🔴 {name} HTTP {status} ({body[:60]})")
    return alerts


def check_dbs() -> list[str]:
    alerts: list[str] = []
    for name, dsn in DSNS.items():
        if not dsn:
            continue
        rows = q(dsn, "SELECT pg_database_size(current_database())/1024/1024 AS mb")
        if not rows or rows[0].get("_error"):
            continue
        mb = int(rows[0]["mb"])
        pct = mb / DB_LIMIT_MB * 100
        record_metric(f"db.size_mb.{name}", mb)
        if pct >= 95 and should_alert(f"db_full_{name}", 3600):
            alerts.append(f"🔴 DB {name}: {mb} MB ({pct:.0f}%) — CRITICAL")
    return alerts


def check_llm_chain() -> list[str]:
    successes = 0
    total = 0
    for name, env_key, url in LLM_PROVIDERS:
        if not os.environ.get(env_key):
            continue
        total += 1
        ok, _, _ = llm_probe(name, env_key, url, timeout=6)
        if ok:
            successes += 1
    if total == 0:
        return []
    record_metric("llm.chain.alive_ratio",
                   round(successes / total, 2),
                   {"alive": successes, "total": total})
    if successes == 0 and should_alert("llm_chain_outage", 3600):
        return [f"🔴 LLM chain TOTAL OUTAGE — {total}/{total} providers failed"]
    return []


def check_staging() -> list[str]:
    rows = q(DSNS["resale"], """
        SELECT COUNT(*) AS pending FROM listings_staging
        WHERE status NOT IN ('promoted','rejected','spam','duplicate')
    """)
    if not rows or rows[0].get("_error"):
        return []
    pending = int(rows[0].get("pending") or 0)
    record_metric("staging.pending", pending)
    if pending > STAGING_HARD and should_alert("staging_hard", 3600):
        return [f"🔴 Staging backlog HARD: {pending} > {STAGING_HARD}"]
    return []


def main() -> int:
    t0 = time.time()
    ensure_schema()
    alerts: list[str] = []
    for fn in (check_parser_intake, check_bots, check_dbs,
               check_llm_chain, check_staging):
        try:
            alerts.extend(fn())
        except Exception as e:
            print(f"[realtime] {fn.__name__} crashed: {e}")
    if alerts:
        # Escape angle brackets so Telegram HTML parser doesn't choke on
        # error messages that may contain '<' or '>'.
        import html
        body = "🚨 <b>Realtime audit alert</b>\n" + "\n".join(
            html.escape(a) for a in alerts
        )
        admin_notify(body, priority="critical")
    dt = time.time() - t0
    record_metric("cron.tick.audit_realtime", round(dt, 2),
                   {"alerts": len(alerts)})
    print(f"[realtime] done {dt:.1f}s alerts={len(alerts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
