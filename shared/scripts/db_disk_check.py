"""Daily disk-usage check for all Railway Postgres DBs.

Alerts admin via Telegram if any DB exceeds:
    >= 90% of 5 GB plan   -> CRITICAL
    >= 80% of 5 GB plan   -> WARNING

Railway hobby/dev plan default volume = 5 GB. Threshold is configurable
via DB_VOLUME_LIMIT_MB env var (defaults to 5000).
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

_SHARED = Path(r"C:\Projects\shared")
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))
try:
    from admin_notify import admin_notify  # type: ignore
except Exception:
    def admin_notify(text: str, **kw) -> bool:  # type: ignore
        print(f"[admin_notify-stub] {text}")
        return False

try:
    import psycopg2  # type: ignore
except ImportError:
    print("psycopg2 not installed: pip install psycopg2-binary", file=sys.stderr)
    sys.exit(2)


_FALLBACK = {
    "resale":       os.environ.get("RESALE_DATABASE_URL", ""),
    "live":         os.environ.get("LIVE_DATABASE_URL", ""),
    "archive":      os.environ.get("ARCHIVE_DATABASE_URL", ""),
    "intelligence": os.environ.get("INTELLIGENCE_DATABASE_URL", ""),
}

DBS = {
    name: os.environ.get(f"{name.upper()}_DATABASE_URL_PUBLIC") or _FALLBACK[name]
    for name in _FALLBACK
}

LIMIT_MB = int(os.environ.get("DB_VOLUME_LIMIT_MB", "5000"))
WARN_PCT = 80
CRIT_PCT = 90


def db_size_mb(dsn: str) -> int | None:
    try:
        conn = psycopg2.connect(dsn, connect_timeout=10)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_database_size(current_database()) / 1024 / 1024")
                row = cur.fetchone()
                return int(row[0]) if row else None
        finally:
            conn.close()
    except Exception as e:
        print(f"  err: {e}")
        return None


report_lines: list[str] = []
alert_lines: list[str] = []

for name, dsn in DBS.items():
    if not dsn:
        continue
    print(f"[check] {name}")
    size_mb = db_size_mb(dsn)
    if size_mb is None:
        report_lines.append(f"  ✗ {name}: connect failed")
        continue
    pct = (size_mb / LIMIT_MB) * 100
    flag = "OK"
    if pct >= CRIT_PCT:
        flag = "🔴 CRIT"
        alert_lines.append(f"🔴 {name}: {size_mb} MB ({pct:.0f}% of {LIMIT_MB} MB) — IMMEDIATE ACTION")
    elif pct >= WARN_PCT:
        flag = "🟡 WARN"
        alert_lines.append(f"🟡 {name}: {size_mb} MB ({pct:.0f}% of {LIMIT_MB} MB) — extend volume soon")
    line = f"  {flag} {name}: {size_mb} MB ({pct:.0f}%)"
    print(line)
    report_lines.append(line)

print("\n=== Disk-usage report ===")
print("\n".join(report_lines))

if alert_lines:
    msg = "DB volume alert\n" + "\n".join(alert_lines)
    admin_notify(msg, priority="critical")
    sys.exit(1)
else:
    # Send a quiet info-level digest once a day so the absence of alerts
    # is also observable.
    admin_notify("✅ db_disk_check OK\n" + "\n".join(report_lines), priority="info")
    sys.exit(0)
