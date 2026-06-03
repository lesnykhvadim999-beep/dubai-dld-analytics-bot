"""Weekly optimizer: recompute meta_strategy_weights from meta_learning_decisions.

Simple statistics, NOT deep RL:
  - trials = count of decisions with outcome_score IS NOT NULL
  - wins   = count where outcome_score >= 0.7
  - avg_score = mean(outcome_score)
  - avg_latency_ms = mean(latency_ms)
  - weight = clip( avg_score * (1 + log(trials+1)/10), 0.1, 5.0 )

Run via cron: every Sunday 03:00 local.

CLI:
    python -m shared.meta_learner.optimizer            # all families
    python -m shared.meta_learner.optimizer --window 30  # last 30 days
"""
from __future__ import annotations

import argparse
import logging
import math
import sys

from ._db import conn_cursor, ensure_schema

log = logging.getLogger(__name__)


def recompute(window_days: int = 14) -> dict:
    ensure_schema()
    summary = {"families": 0, "arms_updated": 0}

    with conn_cursor(dict_cursor=True) as (_, cur):
        cur.execute(
            """
            SELECT
              strategy_family,
              task_type,
              chosen_strategy,
              COUNT(*) FILTER (WHERE outcome_score IS NOT NULL) AS trials,
              COUNT(*) FILTER (WHERE outcome_score >= 0.7)      AS wins,
              AVG(outcome_score)                                 AS avg_score,
              AVG(latency_ms)                                    AS avg_latency
            FROM meta_learning_decisions
            WHERE decided_at >= NOW() - (%s || ' days')::interval
            GROUP BY strategy_family, task_type, chosen_strategy
            """,
            (str(window_days),),
        )
        rows = cur.fetchall()

    if not rows:
        log.info("optimizer: no decisions in window %sd", window_days)
        return summary

    seen_families = set()
    with conn_cursor() as (_, cur):
        for r in rows:
            trials = int(r["trials"] or 0)
            wins = int(r["wins"] or 0)
            avg_score = float(r["avg_score"] or 0.5)
            avg_lat = float(r["avg_latency"] or 0.0)

            if trials == 0:
                continue

            weight = avg_score * (1.0 + math.log(trials + 1) / 10.0)
            weight = max(0.1, min(5.0, weight))

            cur.execute(
                """
                INSERT INTO meta_strategy_weights
                  (strategy_family, task_type, strategy,
                   trials, wins, avg_score, avg_latency_ms, weight, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (strategy_family, task_type, strategy)
                DO UPDATE SET
                  trials = EXCLUDED.trials,
                  wins = EXCLUDED.wins,
                  avg_score = EXCLUDED.avg_score,
                  avg_latency_ms = EXCLUDED.avg_latency_ms,
                  weight = EXCLUDED.weight,
                  updated_at = NOW()
                """,
                (
                    r["strategy_family"], r["task_type"], r["chosen_strategy"],
                    trials, wins, avg_score, avg_lat, weight,
                ),
            )
            summary["arms_updated"] += 1
            seen_families.add(r["strategy_family"])

    summary["families"] = len(seen_families)
    log.info(
        "optimizer: updated %s arms across %s families (window=%sd)",
        summary["arms_updated"], summary["families"], window_days,
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--window", type=int, default=14, help="window in days (default 14)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = recompute(window_days=args.window)
    print(f"meta_learner.optimizer: updated {s['arms_updated']} arms / {s['families']} families")
    return 0


if __name__ == "__main__":
    sys.exit(main())
