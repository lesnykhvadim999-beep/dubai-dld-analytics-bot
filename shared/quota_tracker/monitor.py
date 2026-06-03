"""Read-side: aggregate api_usage_log + decide throttling."""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from ._db import sync_connect
from .limits import LIMITS, _canonical, get_limit

log = logging.getLogger("quota_tracker.monitor")

_WINDOWS = {
    "1m":  timedelta(minutes=1),
    "1h":  timedelta(hours=1),
    "1d":  timedelta(days=1),
    "24h": timedelta(hours=24),
}


def _window_delta(window: str) -> timedelta:
    return _WINDOWS.get(window, timedelta(hours=1))


def current_usage(provider: str, window: str = "1h") -> Dict[str, Any]:
    """Returns dict: rpm, tokens_total, success_rate, pct_to_limit."""
    canonical = _canonical(provider)
    out: Dict[str, Any] = {
        "provider": canonical,
        "window": window,
        "requests": 0,
        "rpm": 0.0,
        "tokens_total": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "success_rate": 1.0,
        "pct_to_limit": 0.0,
        "limit_basis": None,
    }
    conn = sync_connect()
    if conn is None:
        return out
    try:
        delta = _window_delta(window)
        since = datetime.now(timezone.utc) - delta
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(tokens_in), 0),
                       COALESCE(SUM(tokens_out), 0),
                       COALESCE(SUM(CASE WHEN success THEN 1 ELSE 0 END), 0)
                FROM api_usage_log
                WHERE provider = %s AND called_at >= %s
                """,
                (canonical, since),
            )
            row = cur.fetchone() or (0, 0, 0, 0)
            requests, t_in, t_out, ok = row

        minutes = max(delta.total_seconds() / 60.0, 1e-9)
        out["requests"] = int(requests)
        out["tokens_in"] = int(t_in)
        out["tokens_out"] = int(t_out)
        out["tokens_total"] = int(t_in) + int(t_out)
        out["rpm"] = round(requests / minutes, 3)
        out["success_rate"] = (ok / requests) if requests else 1.0

        # pct_to_limit: pick the tightest applicable limit
        lim = get_limit(canonical)
        pct, basis = _compute_pct_to_limit(
            requests=int(requests),
            tokens=int(t_in) + int(t_out),
            window=window,
            lim=lim,
        )
        out["pct_to_limit"] = pct
        out["limit_basis"] = basis
        return out
    except Exception as e:
        log.warning("current_usage(%s) failed: %s", provider, e)
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _compute_pct_to_limit(*, requests: int, tokens: int, window: str,
                          lim: Dict[str, Optional[int]]) -> tuple[float, Optional[str]]:
    """Picks the binding limit and computes % usage. Returns (pct, basis_name)."""
    candidates: List[tuple[float, str]] = []
    rpm = lim.get("rpm")
    tpm = lim.get("tpm")
    rpd = lim.get("rpd")
    tpd = lim.get("tpd")

    minutes = _window_delta(window).total_seconds() / 60.0
    days = _window_delta(window).total_seconds() / 86400.0

    if rpm:
        cap = rpm * max(minutes, 1.0)
        candidates.append((requests / cap, "rpm"))
    if tpm:
        cap = tpm * max(minutes, 1.0)
        candidates.append((tokens / cap, "tpm"))
    if rpd:
        cap = rpd * max(days, 1.0 / 24.0)  # for 1h window use 1/24 of daily
        candidates.append((requests / cap, "rpd"))
    if tpd:
        cap = tpd * max(days, 1.0 / 24.0)
        candidates.append((tokens / cap, "tpd"))

    if not candidates:
        return 0.0, None
    pct, basis = max(candidates, key=lambda x: x[0])
    return round(pct * 100, 2), basis


def should_throttle(provider: str, *, threshold_pct: float = 90.0,
                    window: str = "1h") -> bool:
    """True when current usage > threshold_pct of binding free-tier limit."""
    usage = current_usage(provider, window=window)
    over = usage.get("pct_to_limit", 0.0) >= threshold_pct
    if over:
        _record_alert(
            provider=_canonical(provider),
            pct=float(usage.get("pct_to_limit", 0.0)),
            basis=str(usage.get("limit_basis") or "n/a"),
        )
    return over


def _record_alert(*, provider: str, pct: float, basis: str) -> None:
    """Write a throttle alert (best-effort)."""
    conn = sync_connect()
    if conn is None:
        return
    level = "critical" if pct >= 100 else ("warn" if pct >= 90 else "info")
    msg = f"{provider} {basis} usage at {pct:.1f}% — Vadim Realty (RERA BRN 65011)"
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO quota_alerts (provider, level, pct_used, message)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (provider, level, pct, msg),
                )
    except Exception as e:
        log.debug("_record_alert swallow: %s", e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def recent_alerts(limit: int = 20) -> List[Dict[str, Any]]:
    conn = sync_connect()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT provider, level, pct_used, message, alerted_at
                FROM quota_alerts
                ORDER BY alerted_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
            return [
                {
                    "provider": r[0],
                    "level": r[1],
                    "pct_used": float(r[2] or 0),
                    "message": r[3],
                    "alerted_at": r[4].isoformat() if r[4] else None,
                }
                for r in rows
            ]
    except Exception as e:
        log.warning("recent_alerts failed: %s", e)
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def overview() -> Dict[str, Dict[str, Any]]:
    """One-shot status of all known providers."""
    out: Dict[str, Dict[str, Any]] = {}
    for provider in LIMITS:
        u = current_usage(provider, window="1h")
        u["throttle"] = u.get("pct_to_limit", 0.0) >= 90.0
        out[provider] = u
    return out
