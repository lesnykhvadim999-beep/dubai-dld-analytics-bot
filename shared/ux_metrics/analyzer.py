"""
shared.ux_metrics.analyzer — daily UX metrics.

API:
    compute_funnel(bot_name, funnel_name, period='1d', variant=None) -> dict
    compute_metric(metric_name, variant='all', period='1d', bot_name=None) -> dict
    compare_variants(metric_name, variant_a, variant_b, period='1d') -> dict

Metrics supported:
    time_to_first_action     median ms from menu_shown → first button_clicked
    actions_per_session      avg distinct event count per (user_id, day)
    error_rate               error_seen / total events
    return_rate              % of users active again next day
    completion_rate          action_completed / menu_shown

Predefined funnels:
    'search'         menu_shown → button_clicked → action_completed
    'pro_menu_v2'    menu_shown(variant=simple_menu_v2) → button_clicked → action_completed

All queries are read-only and use the same DSN chain as tracker.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Any, Dict, List, Optional

from .tracker import _get_conn, ensure_schema, is_enabled  # type: ignore

log = logging.getLogger("ux_metrics.analyzer")


# ── period parsing ─────────────────────────────────────────────────────────
def _period_to_interval(period: str) -> str:
    """'1d' → '1 day', '7d' → '7 days', '24h' → '24 hours'."""
    period = (period or "1d").strip().lower()
    if period.endswith("d"):
        n = int(period[:-1] or 1)
        return f"{n} days"
    if period.endswith("h"):
        n = int(period[:-1] or 1)
        return f"{n} hours"
    if period.endswith("m"):
        n = int(period[:-1] or 1)
        return f"{n} minutes"
    return "1 days"


# ── predefined funnels ─────────────────────────────────────────────────────
FUNNELS: Dict[str, List[str]] = {
    "search":      ["menu_shown", "button_clicked", "action_completed"],
    "pro_menu_v2": ["menu_shown", "button_clicked", "action_completed"],
    "default":     ["menu_shown", "button_clicked", "action_completed"],
}


def compute_funnel(
    bot_name: str,
    funnel_name: str = "default",
    period: str = "1d",
    variant: Optional[str] = None,
) -> Dict[str, Any]:
    """Returns:
        {
          'bot_name': ..., 'funnel': ..., 'period': '1d',
          'steps': [{'step': 'menu_shown', 'users': 120, 'pct_of_top': 100.0}, ...],
          'overall_completion_pct': 24.5,
          'variant': '...' | None,
        }
    """
    out: Dict[str, Any] = {
        "bot_name": bot_name, "funnel": funnel_name, "period": period,
        "variant": variant, "steps": [], "overall_completion_pct": 0.0,
    }
    if not is_enabled():
        out["error"] = "ux_metrics disabled (no DSN / no psycopg2)"
        return out
    ensure_schema()
    steps = FUNNELS.get(funnel_name, FUNNELS["default"])
    interval = _period_to_interval(period)
    conn = _get_conn()
    if conn is None:
        out["error"] = "no db connection"
        return out
    counts: List[int] = []
    try:
        with conn.cursor() as cur:
            for step in steps:
                params = [bot_name, step]
                where_variant = ""
                if variant:
                    where_variant = " AND variant=%s"
                    params.append(variant)
                sql = (
                    "SELECT COUNT(DISTINCT user_id) FROM ux_events "
                    "WHERE bot_name=%s AND event_type=%s "
                    "AND occurred_at >= now() - INTERVAL %s"
                    + where_variant
                )
                params.insert(2, interval)
                cur.execute(sql, tuple(params))
                row = cur.fetchone()
                counts.append(int(row[0]) if row and row[0] is not None else 0)
    except Exception as e:
        log.warning("compute_funnel failed: %s", e)
        out["error"] = str(e)
        return out

    top = counts[0] if counts else 0
    for step, c in zip(steps, counts):
        pct = round(100.0 * c / top, 2) if top else 0.0
        out["steps"].append({"step": step, "users": c, "pct_of_top": pct})
    out["overall_completion_pct"] = (
        round(100.0 * counts[-1] / top, 2) if top else 0.0
    )
    return out


# ── single-metric computations ─────────────────────────────────────────────
def _q(conn, sql: str, params: tuple) -> List[tuple]:
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() or []
    except Exception as e:
        log.warning("ux_metrics query failed: %s", e)
        return []


def compute_metric(
    metric_name: str,
    variant: str = "all",
    period: str = "1d",
    bot_name: Optional[str] = None,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "metric": metric_name, "variant": variant, "period": period,
        "bot_name": bot_name, "value": None, "samples": 0,
    }
    if not is_enabled():
        out["error"] = "ux_metrics disabled"
        return out
    ensure_schema()
    interval = _period_to_interval(period)
    conn = _get_conn()
    if conn is None:
        out["error"] = "no db"
        return out

    var_filter = ""
    var_params: tuple = ()
    if variant and variant != "all":
        var_filter = " AND variant=%s"
        var_params = (variant,)
    bot_filter = ""
    bot_params: tuple = ()
    if bot_name:
        bot_filter = " AND bot_name=%s"
        bot_params = (bot_name,)

    if metric_name == "time_to_first_action":
        sql = f"""
        WITH first_menu AS (
          SELECT user_id, MIN(occurred_at) AS t0
            FROM ux_events
           WHERE event_type='menu_shown'
             AND occurred_at >= now() - INTERVAL %s
             {var_filter} {bot_filter}
           GROUP BY user_id
        ),
        first_click AS (
          SELECT e.user_id, MIN(e.occurred_at) AS t1
            FROM ux_events e
            JOIN first_menu m ON m.user_id=e.user_id
           WHERE e.event_type='button_clicked'
             AND e.occurred_at >= m.t0
             AND e.occurred_at <  m.t0 + INTERVAL '1 hour'
             {var_filter.replace('variant', 'e.variant')}
             {bot_filter.replace('bot_name', 'e.bot_name')}
           GROUP BY e.user_id
        )
        SELECT
          percentile_cont(0.5) WITHIN GROUP
            (ORDER BY EXTRACT(EPOCH FROM (c.t1 - m.t0)) * 1000.0) AS p50_ms,
          COUNT(*) AS samples
          FROM first_menu m
          JOIN first_click c ON m.user_id=c.user_id;
        """
        params = (interval,) + var_params + bot_params + var_params + bot_params
        rows = _q(conn, sql, params)
        if rows:
            v, n = rows[0]
            out["value"] = float(v) if v is not None else None
            out["samples"] = int(n or 0)
            out["unit"] = "ms"
        return out

    if metric_name == "actions_per_session":
        sql = f"""
        SELECT AVG(cnt)::float, COUNT(*)
          FROM (
            SELECT user_id, DATE(occurred_at) AS d,
                   COUNT(*) AS cnt
              FROM ux_events
             WHERE occurred_at >= now() - INTERVAL %s
               {var_filter} {bot_filter}
             GROUP BY user_id, DATE(occurred_at)
          ) s;
        """
        rows = _q(conn, sql, (interval,) + var_params + bot_params)
        if rows:
            v, n = rows[0]
            out["value"] = float(v) if v is not None else 0.0
            out["samples"] = int(n or 0)
        return out

    if metric_name == "error_rate":
        sql = f"""
        SELECT
          SUM(CASE WHEN event_type='error_seen' THEN 1 ELSE 0 END)::float
            / NULLIF(COUNT(*),0) AS rate,
          COUNT(*) AS total
          FROM ux_events
         WHERE occurred_at >= now() - INTERVAL %s
           {var_filter} {bot_filter};
        """
        rows = _q(conn, sql, (interval,) + var_params + bot_params)
        if rows:
            v, n = rows[0]
            out["value"] = float(v) if v is not None else 0.0
            out["samples"] = int(n or 0)
        return out

    if metric_name == "return_rate":
        # % of distinct users seen on day D−1 who returned on day D
        sql = f"""
        WITH day_users AS (
          SELECT DATE(occurred_at) AS d, user_id
            FROM ux_events
           WHERE occurred_at >= now() - INTERVAL %s
             {var_filter} {bot_filter}
           GROUP BY 1,2
        )
        SELECT
          COUNT(DISTINCT b.user_id)::float
            / NULLIF(COUNT(DISTINCT a.user_id),0) AS rate,
          COUNT(DISTINCT a.user_id) AS samples
          FROM day_users a
          LEFT JOIN day_users b
            ON b.user_id = a.user_id
           AND b.d = a.d + 1;
        """
        rows = _q(conn, sql, (interval,) + var_params + bot_params)
        if rows:
            v, n = rows[0]
            out["value"] = float(v) if v is not None else 0.0
            out["samples"] = int(n or 0)
        return out

    if metric_name == "completion_rate":
        sql = f"""
        WITH menu AS (
          SELECT DISTINCT user_id FROM ux_events
           WHERE event_type='menu_shown'
             AND occurred_at >= now() - INTERVAL %s
             {var_filter} {bot_filter}
        ),
        done AS (
          SELECT DISTINCT user_id FROM ux_events
           WHERE event_type='action_completed'
             AND occurred_at >= now() - INTERVAL %s
             {var_filter} {bot_filter}
        )
        SELECT
          COUNT(DISTINCT d.user_id)::float
            / NULLIF(COUNT(DISTINCT m.user_id),0),
          COUNT(DISTINCT m.user_id)
          FROM menu m
          LEFT JOIN done d USING(user_id);
        """
        params = (interval,) + var_params + bot_params + (interval,) + var_params + bot_params
        rows = _q(conn, sql, params)
        if rows:
            v, n = rows[0]
            out["value"] = float(v) if v is not None else 0.0
            out["samples"] = int(n or 0)
        return out

    out["error"] = f"unknown metric '{metric_name}'"
    return out


# ── A/B comparison ────────────────────────────────────────────────────────
def compare_variants(
    metric_name: str,
    variant_a: str = "legacy_menu",
    variant_b: str = "simple_menu_v2",
    period: str = "1d",
    bot_name: Optional[str] = None,
) -> Dict[str, Any]:
    a = compute_metric(metric_name, variant=variant_a,
                       period=period, bot_name=bot_name)
    b = compute_metric(metric_name, variant=variant_b,
                       period=period, bot_name=bot_name)
    va = a.get("value")
    vb = b.get("value")
    delta_pct: Optional[float] = None
    if va is not None and vb is not None and va not in (0, 0.0):
        # For latency / error_rate: lower is better. For completion / return: higher is better.
        # We report (B - A) / A in percent regardless; sign is interpreted by rollback.
        try:
            delta_pct = round(100.0 * (vb - va) / abs(va), 2)
        except Exception:
            delta_pct = None
    return {
        "metric": metric_name,
        "variant_a": {"name": variant_a, **a},
        "variant_b": {"name": variant_b, **b},
        "delta_pct": delta_pct,
        "period": period,
        "bot_name": bot_name,
    }


# ── CLI ────────────────────────────────────────────────────────────────────
def _print_json(obj):
    import json as _json
    print(_json.dumps(obj, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    import sys
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: python -m shared.ux_metrics.analyzer <cmd> ...")
        print("  funnel <bot> [funnel_name] [period] [variant]")
        print("  metric <name> [variant] [period] [bot]")
        print("  compare <metric> [variant_a] [variant_b] [period] [bot]")
        sys.exit(0)
    cmd = argv[0]
    if cmd == "funnel":
        _print_json(compute_funnel(
            bot_name=argv[1] if len(argv) > 1 else "resale",
            funnel_name=argv[2] if len(argv) > 2 else "default",
            period=argv[3] if len(argv) > 3 else "1d",
            variant=argv[4] if len(argv) > 4 else None,
        ))
    elif cmd == "metric":
        _print_json(compute_metric(
            metric_name=argv[1] if len(argv) > 1 else "completion_rate",
            variant=argv[2] if len(argv) > 2 else "all",
            period=argv[3] if len(argv) > 3 else "1d",
            bot_name=argv[4] if len(argv) > 4 else None,
        ))
    elif cmd == "compare":
        _print_json(compare_variants(
            metric_name=argv[1] if len(argv) > 1 else "completion_rate",
            variant_a=argv[2] if len(argv) > 2 else "legacy_menu",
            variant_b=argv[3] if len(argv) > 3 else "simple_menu_v2",
            period=argv[4] if len(argv) > 4 else "1d",
            bot_name=argv[5] if len(argv) > 5 else None,
        ))
    else:
        print(f"unknown cmd: {cmd}")
        sys.exit(2)
