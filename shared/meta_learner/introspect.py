"""Admin introspection — /meta_stats command.

Renders win rates per (family, task_type, strategy) for the last N days.

Bot integration:
    from shared.meta_learner.introspect import render_meta_stats
    text = render_meta_stats(days=7)
    await bot.send_message(ADMIN_CHAT_ID, text, parse_mode="HTML")
"""
from __future__ import annotations

import logging
from typing import Any

from ._db import conn_cursor

log = logging.getLogger(__name__)


def get_meta_stats(days: int = 7) -> list[dict[str, Any]]:
    try:
        with conn_cursor(dict_cursor=True) as (_, cur):
            cur.execute(
                """
                SELECT
                  strategy_family,
                  task_type,
                  chosen_strategy,
                  COUNT(*) AS total,
                  COUNT(*) FILTER (WHERE outcome_score IS NOT NULL) AS with_outcome,
                  AVG(outcome_score) AS avg_score,
                  AVG(latency_ms)   AS avg_latency,
                  SUM(cost_usd)     AS total_cost
                FROM meta_learning_decisions
                WHERE decided_at >= NOW() - (%s || ' days')::interval
                GROUP BY strategy_family, task_type, chosen_strategy
                ORDER BY strategy_family, task_type, avg_score DESC NULLS LAST
                """,
                (str(days),),
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log.warning("get_meta_stats failed: %s", e)
        return []


def render_meta_stats(days: int = 7) -> str:
    rows = get_meta_stats(days)
    if not rows:
        return f"<b>Meta-Learning stats (last {days}d)</b>\n\nNo decisions yet."

    lines = [f"<b>Meta-Learning stats (last {days}d)</b>", ""]
    current_family = None
    current_task = None
    for r in rows:
        if r["strategy_family"] != current_family:
            current_family = r["strategy_family"]
            current_task = None
            lines.append(f"\n<b>[{current_family}]</b>")
        if r["task_type"] != current_task:
            current_task = r["task_type"]
            lines.append(f"  <i>{current_task}</i>")
        avg = r["avg_score"]
        avg_s = f"{float(avg):.2f}" if avg is not None else "—"
        lat = r["avg_latency"]
        lat_s = f"{int(lat)}ms" if lat else "—"
        lines.append(
            f"    {r['chosen_strategy']}: "
            f"score={avg_s} lat={lat_s} "
            f"n={r['with_outcome']}/{r['total']}"
        )

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    print(render_meta_stats(days))
