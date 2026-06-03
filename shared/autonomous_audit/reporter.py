"""PHASE BN N4 — Daily digest reporter.

`daily_digest()` aggregates findings from `audit_findings` in the last 24h
and sends a single HTML message to @vadim_admin_bot.

Format:
  📋 Audit Digest — YYYY-MM-DD
  Runs: hourly=24, daily=1, weekly=0
  Findings: CRITICAL=N, HIGH=N, MEDIUM=N, LOW=N
  Top categories:
   • brand (2)
   • heartbeat (1)
  Recent CRITICAL/HIGH:
   • [HIGH] heartbeat — resale-bot stale 12.3 min
   • [CRITICAL] brand — forbidden brand name found in 3 files
  Unresolved findings older than 7d: N

CLI:
  python -m shared.autonomous_audit.reporter
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SHARED = _HERE.parent
_PROJECT = _SHARED.parent
for p in (str(_PROJECT), str(_SHARED)):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    import psycopg2  # type: ignore
    import psycopg2.extras  # type: ignore
    _PG_OK = True
except Exception:
    _PG_OK = False

try:
    from admin_notify import admin_notify  # type: ignore
except Exception:
    def admin_notify(text: str, **kw) -> bool:  # type: ignore
        print(f"[admin_notify-stub] {text}")
        return False

log = logging.getLogger("autonomous_audit.reporter")


def _dsn() -> str:
    for k in ("RESALE_DATABASE_URL_PUBLIC", "RESALE_DATABASE_URL",
              "INTELLIGENCE_DATABASE_URL_PUBLIC", "INTELLIGENCE_DATABASE_URL",
              "LIVE_DATABASE_URL_PUBLIC", "LIVE_DATABASE_URL",
              "DATABASE_URL"):
        v = os.environ.get(k)
        if v:
            return v
    return ""


def _q(dsn: str, sql: str, params: tuple = ()) -> list[dict]:
    if not dsn or not _PG_OK:
        return []
    try:
        with psycopg2.connect(dsn, connect_timeout=8) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                if cur.description:
                    return [dict(r) for r in cur.fetchall()]
                return []
    except Exception as e:
        log.warning("_q failed: %s", e)
        return []


def daily_digest(notify: bool = True) -> dict:
    """Collect last 24h findings → admin message. Returns summary dict."""
    dsn = _dsn()
    if not dsn:
        return {"status": "no_dsn"}

    runs = _q(dsn, """
        SELECT audit_type, COUNT(*) AS n,
               COALESCE(SUM(findings_count), 0) AS findings
        FROM public.audit_runs
        WHERE started_at > NOW() - INTERVAL '24 hours'
        GROUP BY audit_type
    """)
    sev = _q(dsn, """
        SELECT UPPER(COALESCE(severity, 'LOW')) AS sev, COUNT(*) AS n
        FROM public.audit_findings
        WHERE discovered_at > NOW() - INTERVAL '24 hours'
        GROUP BY 1
    """)
    by_cat = _q(dsn, """
        SELECT category, COUNT(*) AS n
        FROM public.audit_findings
        WHERE discovered_at > NOW() - INTERVAL '24 hours'
        GROUP BY category
        ORDER BY n DESC
        LIMIT 8
    """)
    recent = _q(dsn, """
        SELECT severity, category, description, discovered_at
        FROM public.audit_findings
        WHERE discovered_at > NOW() - INTERVAL '24 hours'
          AND UPPER(severity) IN ('CRITICAL', 'HIGH')
        ORDER BY
          CASE UPPER(severity)
            WHEN 'CRITICAL' THEN 0
            WHEN 'HIGH'     THEN 1
            ELSE 2 END,
          discovered_at DESC
        LIMIT 12
    """)
    unresolved = _q(dsn, """
        SELECT COUNT(*) AS n
        FROM public.audit_findings
        WHERE resolved_at IS NULL
          AND discovered_at < NOW() - INTERVAL '7 days'
    """)

    sev_map = {r["sev"]: int(r["n"]) for r in sev}
    run_map = {r["audit_type"]: (int(r["n"]), int(r["findings"])) for r in runs}
    unr = int((unresolved[0].get("n") if unresolved else 0) or 0)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    parts: list[str] = [f"<b>📋 Audit Digest — {today}</b>"]
    parts.append(
        "Runs: " + ", ".join(
            f"{t}={run_map.get(t, (0, 0))[0]}"
            for t in ("hourly", "daily", "weekly")
        )
    )
    parts.append(
        "Findings: "
        f"CRITICAL={sev_map.get('CRITICAL', 0)}, "
        f"HIGH={sev_map.get('HIGH', 0)}, "
        f"MEDIUM={sev_map.get('MEDIUM', 0)}, "
        f"LOW={sev_map.get('LOW', 0)}"
    )
    if by_cat:
        parts.append("\n<b>Top categories</b>:")
        for r in by_cat:
            parts.append(f" • {r['category']} ({r['n']})")
    if recent:
        parts.append("\n<b>Recent CRITICAL/HIGH</b>:")
        for r in recent:
            desc = (r.get("description") or "")[:160]
            parts.append(
                f" • [{r.get('severity')}] {r.get('category')} — {desc}"
            )
    if unr:
        parts.append(f"\n⚠️ Unresolved findings older than 7d: <b>{unr}</b>")

    # PHASE BN/BO Audit B3 — brand footer for all admin digests
    parts.append("\n<i>Vadim Realty | RERA BRN 65011</i>")
    msg = "\n".join(parts)
    sent = False
    if notify:
        try:
            sent = admin_notify(msg, parse_mode="HTML", priority="normal")
        except Exception as e:
            log.warning("admin_notify failed: %s", e)

    return {
        "status": "ok",
        "runs": run_map,
        "severity": sev_map,
        "categories": [(r["category"], int(r["n"])) for r in by_cat],
        "unresolved_old": unr,
        "notified": sent,
        "message_preview": msg[:300],
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--no-notify", action="store_true",
                   help="Print digest instead of sending to admin")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    summary = daily_digest(notify=not args.no_notify)
    print(f"[reporter] {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
