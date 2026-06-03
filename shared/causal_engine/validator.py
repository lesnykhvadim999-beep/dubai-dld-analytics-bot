"""Backtest causal_facts against historical DLD data.

For each fact:
  - if cause is observable in DLD archive (e.g. "off-plan handover wave"
    -> tx_count of handover_type in a quarter > threshold),
  - check whether the predicted effect (price/rent change) materialized
    in the following 1-2 quarters.

Updates causal_facts.last_validated, .validation_score, and adjusts
.confidence by exponential moving average toward the observed hit-rate.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, Any, List

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

DSN = os.getenv(
    "INTELLIGENCE_DB_DSN",
    os.environ.get("LIVE_DATABASE_URL") or os.environ.get("DATABASE_URL") or os.environ.get("INTELLIGENCE_DATABASE_URL") or "",
)

EMA_ALPHA = 0.25  # how much the new measurement shifts confidence


# Mapping fact substring -> backtest SQL.  Only a few "machine-checkable"
# patterns are implemented; the rest get a passive last_validated stamp.
BACKTESTS = [
    {
        "keywords": ["off-plan handover wave"],
        "sql": """
            -- Did quarters with > N handover-related transactions in an area
            -- show a subsequent price drop?  Returns hit-rate (0..1).
            WITH waves AS (
                SELECT area_en, date_trunc('quarter', instance_date) q, COUNT(*) n
                FROM dld_transactions
                WHERE meta_real_property_type_en ILIKE '%off plan%'
                GROUP BY 1,2
                HAVING COUNT(*) > 1500
            ),
            nextq AS (
                SELECT w.area_en, w.q,
                    (SELECT AVG(actual_worth)
                       FROM dld_transactions t
                      WHERE t.area_en = w.area_en
                        AND date_trunc('quarter', t.instance_date) = w.q + INTERVAL '3 months') as p_next,
                    (SELECT AVG(actual_worth)
                       FROM dld_transactions t
                      WHERE t.area_en = w.area_en
                        AND date_trunc('quarter', t.instance_date) = w.q) as p_now
                FROM waves w
            )
            SELECT
                AVG(CASE WHEN p_next < p_now * 0.97 THEN 1.0 ELSE 0.0 END) AS hit_rate,
                COUNT(*) AS n_obs
            FROM nextq
            WHERE p_now IS NOT NULL AND p_next IS NOT NULL
        """,
    },
    {
        "keywords": ["summer months", "summer (jun-aug)"],
        "sql": """
            WITH summer AS (
                SELECT EXTRACT(year FROM instance_date) y, COUNT(*) n
                FROM dld_transactions
                WHERE EXTRACT(month FROM instance_date) BETWEEN 6 AND 8
                GROUP BY 1
            ),
            spring AS (
                SELECT EXTRACT(year FROM instance_date) y, COUNT(*) n
                FROM dld_transactions
                WHERE EXTRACT(month FROM instance_date) BETWEEN 3 AND 5
                GROUP BY 1
            )
            SELECT
                AVG(CASE WHEN s.n < sp.n * 0.85 THEN 1.0 ELSE 0.0 END) AS hit_rate,
                COUNT(*) AS n_obs
            FROM summer s JOIN spring sp ON s.y = sp.y
        """,
    },
]


def _run_one(cur, fact: Dict[str, Any]) -> Dict[str, Any]:
    cause_lc = (fact["cause_text"] or "").lower()
    for bt in BACKTESTS:
        if any(k in cause_lc for k in bt["keywords"]):
            try:
                cur.execute(bt["sql"])
                row = cur.fetchone()
                if row and row.get("n_obs", 0) and row.get("n_obs") > 0:
                    return {"hit_rate": float(row["hit_rate"] or 0), "n_obs": int(row["n_obs"])}
            except Exception as e:
                logger.warning("backtest sql failed for fact %s: %s", fact["id"], e)
                return {"hit_rate": None, "n_obs": 0, "error": str(e)[:200]}
    return {"hit_rate": None, "n_obs": 0}


def backtest(limit: int = 200) -> Dict[str, Any]:
    out = {"checked": 0, "with_data": 0, "updated": 0}
    try:
        with psycopg2.connect(DSN) as conn:
            conn.autocommit = True
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, cause_text, effect_text, confidence "
                    "FROM causal_facts ORDER BY id LIMIT %s", (limit,)
                )
                facts = cur.fetchall()
                for f in facts:
                    out["checked"] += 1
                    res = _run_one(cur, dict(f))
                    if res["hit_rate"] is not None:
                        out["with_data"] += 1
                        new_score = float(res["hit_rate"])
                        new_conf = (1 - EMA_ALPHA) * float(f["confidence"]) + EMA_ALPHA * new_score
                        cur.execute(
                            """UPDATE causal_facts
                               SET validation_score = %s,
                                   confidence       = %s,
                                   last_validated   = NOW()
                               WHERE id = %s""",
                            (new_score, max(0.05, min(0.99, new_conf)), f["id"]),
                        )
                        out["updated"] += 1
                    else:
                        cur.execute(
                            "UPDATE causal_facts SET last_validated = NOW() WHERE id = %s",
                            (f["id"],),
                        )
    except Exception as e:
        logger.error("validator failed: %s", e)
        out["error"] = str(e)[:200]
    print(f"[validator] {out}")
    return out


if __name__ == "__main__":
    backtest()
