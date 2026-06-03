"""PHASE BO O2 — System health digest (hourly to admin chat).

DSN sources (env only):
  INTELLIGENCE_DATABASE_URL_PUBLIC > INTELLIGENCE_DATABASE_URL >
  DATABASE_URL > RESALE_DATABASE_URL

Output of build_telegram_digest:

  System pulse (HH:MM)  <score>/100
  Bots: 5/6 alive
  LLM providers: all OK
  Features: 32 enabled / 0 disabled
  Alerts: 0 critical / 0 pending
  Users 24h: 47
  ---
  Vadim Realty (RERA BRN 65011)

If health_score < 80 -> send_digest() pushes via shared.admin_notify
with priority='critical'. Failure-tolerant: missing v_system_health
or psycopg2 unavailable -> returns empty/zero dict, never raises.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

# DSN env lookup (env vars only -- no hardcoded credentials)
_DSN_ENV_KEYS = (
    "INTELLIGENCE_DATABASE_URL_PUBLIC",
    "INTELLIGENCE_DATABASE_URL",
    "DATABASE_URL",
    "RESALE_DATABASE_URL",
)


def _dsn() -> Optional[str]:
    for k in _DSN_ENV_KEYS:
        v = os.environ.get(k)
        if v:
            return v
    return None


# Default snapshot (used on any error)
_EMPTY = {
    "bots_alive_5min": 0,
    "bots_total": 0,
    "providers_down": 0,
    "features_enabled": 0,
    "features_disabled": 0,
    "critical_findings": 0,
    "pending_incidents": 0,
    "active_users_24h": 0,
    "computed_at": None,
    "ok": False,
}


def fetch_system_health() -> dict:
    """Read v_system_health row. Returns dict with 'ok'=True on success.
    Never raises; on any failure returns _EMPTY with ok=False.
    """
    dsn = _dsn()
    if not dsn:
        return dict(_EMPTY)
    try:
        import psycopg2  # type: ignore
        import psycopg2.extras  # type: ignore
    except Exception:
        return dict(_EMPTY)
    try:
        with psycopg2.connect(dsn, connect_timeout=5) as conn:
            conn.autocommit = True
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM v_system_health")
                row = cur.fetchone()
                if not row:
                    return dict(_EMPTY)
                out = dict(_EMPTY)
                out.update(row)
                out["ok"] = True
                return out
    except Exception as e:
        out = dict(_EMPTY)
        out["error"] = str(e)[:200]
        return out


def compute_health_score(snapshot: Optional[dict] = None) -> int:
    """Compute 0-100 health score.

    Start at 100, subtract penalties:
      - each provider OPEN          : -10
      - each critical finding       : -8
      - each pending incident       : -5
      - each disabled feature       : -3
      - bots offline (total - alive): -10 per missing bot
      - failed to fetch snapshot    : score = 0
    Clamped to [0, 100].
    """
    if snapshot is None:
        snapshot = fetch_system_health()
    if not snapshot.get("ok"):
        return 0
    score = 100
    score -= 10 * int(snapshot.get("providers_down", 0) or 0)
    score -= 8 * int(snapshot.get("critical_findings", 0) or 0)
    score -= 5 * int(snapshot.get("pending_incidents", 0) or 0)
    score -= 3 * int(snapshot.get("features_disabled", 0) or 0)
    bots_total = int(snapshot.get("bots_total", 0) or 0)
    bots_alive = int(snapshot.get("bots_alive_5min", 0) or 0)
    missing_bots = max(0, bots_total - bots_alive)
    score -= 10 * missing_bots
    return max(0, min(100, score))


def _fmt_bots(snapshot: dict) -> str:
    alive = int(snapshot.get("bots_alive_5min", 0) or 0)
    total = int(snapshot.get("bots_total", 0) or 0)
    if total == 0:
        tag = "(no heartbeats)"
    elif alive == total:
        tag = "OK"
    elif alive > 0:
        tag = "DEGRADED"
    else:
        tag = "DOWN"
    return f"Bots: {alive}/{total} alive [{tag}]"


def _fmt_providers(snapshot: dict) -> str:
    open_n = int(snapshot.get("providers_down", 0) or 0)
    if open_n == 0:
        return "LLM providers: all OK"
    return f"LLM providers: {open_n} OPEN"


def _fmt_features(snapshot: dict) -> str:
    on = int(snapshot.get("features_enabled", 0) or 0)
    off = int(snapshot.get("features_disabled", 0) or 0)
    return f"Features: {on} enabled / {off} disabled"


def _fmt_alerts(snapshot: dict) -> str:
    crit = int(snapshot.get("critical_findings", 0) or 0)
    pend = int(snapshot.get("pending_incidents", 0) or 0)
    return f"Alerts: {crit} critical / {pend} pending"


def build_telegram_digest(snapshot: Optional[dict] = None) -> str:
    """Build the formatted digest string for Telegram (ASCII-safe)."""
    if snapshot is None:
        snapshot = fetch_system_health()
    score = compute_health_score(snapshot)
    hhmm = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines = [
        f"System pulse ({hhmm}) -- score {score}/100",
        _fmt_bots(snapshot),
        _fmt_providers(snapshot),
        _fmt_features(snapshot),
        _fmt_alerts(snapshot),
        f"Users 24h: {int(snapshot.get('active_users_24h', 0) or 0)}",
    ]
    if not snapshot.get("ok"):
        lines.append("WARN: snapshot fetch failed -- DSN or view missing")
    lines.append("---")
    lines.append("Vadim Realty (RERA BRN 65011)")
    return "\n".join(lines)


def send_digest(force: bool = False) -> bool:
    """Build digest and send to admin chat.

    Returns True if admin_notify accepted the message.
    Critical (score < 80) sends with priority='critical' so it bypasses
    ADMIN_NOTIFY_QUIET. Caller is expected to be the hourly cron.
    """
    snapshot = fetch_system_health()
    score = compute_health_score(snapshot)
    text = build_telegram_digest(snapshot)
    priority = "critical" if score < 80 else "normal"
    try:
        from shared.admin_notify import admin_notify  # type: ignore
    except Exception:
        try:
            from admin_notify import admin_notify  # type: ignore
        except Exception:
            print(text)
            return False
    return admin_notify(text, parse_mode="HTML", priority=priority)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "send":
        ok = send_digest(force=True)
        print("[observability] send_digest ok=", ok)
    else:
        snap = fetch_system_health()
        print("snapshot:", snap)
        print("score:", compute_health_score(snap))
        print("---digest---")
        print(build_telegram_digest(snap))
