"""
behavior_tracking.analytics — read-side helpers for daily audit / dashboards.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .tracker import _get_conn, is_enabled


def daily_health(window_hours: int = 24) -> List[Dict[str, Any]]:
    """
    Returns per-bot health rows over last N hours.
    """
    if not is_enabled():
        return []
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  bot_name,
                  COUNT(*) AS total,
                  COUNT(DISTINCT user_id) AS users,
                  COUNT(*) FILTER (WHERE outcome='success') AS ok,
                  COUNT(*) FILTER (WHERE outcome='empty')   AS empty,
                  COUNT(*) FILTER (WHERE outcome='error')   AS err,
                  ROUND(100.0 * COUNT(*) FILTER (WHERE outcome='success')
                        / NULLIF(COUNT(*),0), 1) AS success_pct,
                  ROUND(100.0 * COUNT(*) FILTER (WHERE outcome='empty')
                        / NULLIF(COUNT(*),0), 1) AS empty_pct,
                  PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,
                  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
                FROM bot_interactions
                WHERE ts > NOW() - (%s || ' hours')::interval
                GROUP BY 1
                ORDER BY total DESC;
                """,
                (str(int(window_hours)),),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        return []


def feedback_summary(window_hours: int = 24) -> List[Dict[str, Any]]:
    if not is_enabled():
        return []
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  bot_name,
                  COUNT(*) AS total,
                  SUM(CASE WHEN rating= 1 THEN 1 ELSE 0 END) AS pos,
                  SUM(CASE WHEN rating=-1 THEN 1 ELSE 0 END) AS neg,
                  SUM(CASE WHEN rating= 0 THEN 1 ELSE 0 END) AS neutral,
                  ROUND(100.0 * SUM(CASE WHEN rating=1 THEN 1 ELSE 0 END)
                        / NULLIF(COUNT(*),0), 1) AS satisfaction_pct
                FROM bot_feedback
                WHERE ts > NOW() - (%s || ' hours')::interval
                GROUP BY 1
                ORDER BY total DESC;
                """,
                (str(int(window_hours)),),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        return []


def empty_rate_anomalies(*, lookback_days: int = 14, sigma: float = 2.0) -> List[Dict[str, Any]]:
    """
    Per-bot: today's empty rate vs trailing mean+σ.
    """
    if not is_enabled():
        return []
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH daily AS (
                  SELECT bot_name, DATE_TRUNC('day', ts) AS d,
                         COUNT(*) AS n,
                         COUNT(*) FILTER (WHERE outcome='empty')::float / NULLIF(COUNT(*),0) AS er
                  FROM bot_interactions
                  WHERE ts > NOW() - (%s || ' days')::interval
                  GROUP BY 1,2
                ),
                stats AS (
                  SELECT bot_name,
                         AVG(er) AS mu, STDDEV_POP(er) AS sd
                  FROM daily
                  WHERE d < DATE_TRUNC('day', NOW())
                  GROUP BY 1
                ),
                today AS (
                  SELECT bot_name, er
                  FROM daily
                  WHERE d = DATE_TRUNC('day', NOW())
                )
                SELECT t.bot_name,
                       ROUND(100*t.er,1) AS today_pct,
                       ROUND(100*COALESCE(s.mu,0),1) AS mean_pct,
                       ROUND(100*COALESCE(s.sd,0),1) AS sd_pct
                FROM today t
                LEFT JOIN stats s USING (bot_name)
                WHERE COALESCE(s.sd,0) > 0
                  AND t.er > COALESCE(s.mu,0) + %s * COALESCE(s.sd,0);
                """,
                (str(int(lookback_days)), float(sigma)),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        return []


def render_audit_section() -> str:
    """
    String block for daily_audit.py integration.
    """
    health = daily_health(24)
    fb = {r["bot_name"]: r for r in feedback_summary(24)}
    anomalies = empty_rate_anomalies()
    if not health and not fb:
        return "🎯 USER FEEDBACK — нет данных за 24ч"
    lines = ["🎯 USER FEEDBACK / BEHAVIOR (last 24h)"]
    for h in health:
        bot = h["bot_name"]
        f = fb.get(bot)
        fb_part = ""
        if f and f["total"]:
            fb_part = f" | {int(f['pos'])}👍 / {int(f['neg'])}👎 / sat {f['satisfaction_pct']}%"
        lines.append(
            f"├ {bot}: {h['total']} req · {h['users']} users · "
            f"ok {h['success_pct']}% · empty {h['empty_pct']}% · "
            f"p95 {int(h['p95_ms'] or 0)}ms{fb_part}"
        )
    if anomalies:
        lines.append("└ ⚠️ EMPTY-RATE anomalies:")
        for a in anomalies:
            lines.append(f"   • {a['bot_name']}: {a['today_pct']}% (avg {a['mean_pct']}% ±{a['sd_pct']}%)")
    else:
        lines.append("└ ✓ no empty-rate anomalies")
    return "\n".join(lines)
