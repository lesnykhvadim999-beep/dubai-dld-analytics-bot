# -*- coding: utf-8 -*-
"""builder_living — rank Dubai areas for the 'Living' goal.

Hybrid: curated static lifestyle scores (schools/safety/amenities/family/walk)
combined with DLD-derived price_stability (low volatility = good for living).

Score = weighted(schools=25%, safety=20%, amenities=15%, family=20%, walk=10%, stability=10%).
"""
from __future__ import annotations
import logging
import statistics
import time

from ._common import (
    AREA_CATALOG, get_dld_conn, get_intel_conn, upsert_ranking, log_refresh,
)

log = logging.getLogger("area_rankings.living")

# Curated lifestyle baselines (0-10). Manually researched, refreshed in code.
LIVING_CURATED: dict[str, dict] = {
    "Downtown Dubai":           {"schools": 7,  "safety": 9,  "amenities": 10, "walk": 9, "family": 5},
    "Dubai Marina":             {"schools": 7,  "safety": 8,  "amenities": 9,  "walk": 9, "family": 6},
    "Jumeirah Village Circle":  {"schools": 8,  "safety": 9,  "amenities": 7,  "walk": 5, "family": 9},
    "Arabian Ranches":          {"schools": 10, "safety": 10, "amenities": 7,  "walk": 3, "family": 10},
    "Palm Jumeirah":            {"schools": 6,  "safety": 9,  "amenities": 10, "walk": 7, "family": 7},
    "Mirdif":                   {"schools": 9,  "safety": 10, "amenities": 7,  "walk": 6, "family": 10},
    "Damac Hills":              {"schools": 8,  "safety": 9,  "amenities": 7,  "walk": 5, "family": 9},
    "Dubai Hills Estate":       {"schools": 9,  "safety": 9,  "amenities": 8,  "walk": 7, "family": 9},
    "JBR":                      {"schools": 6,  "safety": 8,  "amenities": 9,  "walk": 9, "family": 6},
    "Town Square":              {"schools": 8,  "safety": 9,  "amenities": 6,  "walk": 4, "family": 10},
    "Sobha Hartland":           {"schools": 8,  "safety": 9,  "amenities": 8,  "walk": 6, "family": 8},
    "Al Barsha":                {"schools": 8,  "safety": 8,  "amenities": 7,  "walk": 6, "family": 8},
    "Tilal Al Ghaf":            {"schools": 9,  "safety": 10, "amenities": 7,  "walk": 5, "family": 10},
    "Business Bay":             {"schools": 6,  "safety": 8,  "amenities": 9,  "walk": 8, "family": 5},
    "JLT":                      {"schools": 7,  "safety": 8,  "amenities": 8,  "walk": 8, "family": 6},
    "DAMAC Lagoons":            {"schools": 8,  "safety": 9,  "amenities": 7,  "walk": 4, "family": 9},
    "MBR City":                 {"schools": 8,  "safety": 9,  "amenities": 8,  "walk": 6, "family": 8},
    "Dubai Creek Harbour":      {"schools": 7,  "safety": 9,  "amenities": 8,  "walk": 7, "family": 7},
    "Bluewaters":               {"schools": 6,  "safety": 9,  "amenities": 9,  "walk": 8, "family": 6},
    "Dubai South":              {"schools": 7,  "safety": 9,  "amenities": 6,  "walk": 4, "family": 8},
}

# Volatility windows (months → AVG meter_sale_price). 8 quarters = 2 years.
SQL_VOLATILITY = """
WITH q AS (
  SELECT
    DATE_TRUNC('quarter', TO_DATE(NULLIF(instance_date,''),'YYYY-MM-DD')) AS qd,
    AVG(NULLIF(meter_sale_price,'')::numeric)                              AS psqft
  FROM public.dld_sales_unified
  WHERE area_name_en = ANY(%s)
    AND COALESCE(procedure_name_en,'') NOT ILIKE '%%mortgage%%'
    AND NULLIF(meter_sale_price,'') IS NOT NULL
    AND NULLIF(instance_date,'') IS NOT NULL
    AND TO_DATE(NULLIF(instance_date,''),'YYYY-MM-DD') >= CURRENT_DATE - INTERVAL '24 months'
  GROUP BY 1
  HAVING AVG(NULLIF(meter_sale_price,'')::numeric) > 0
)
SELECT qd, psqft FROM q ORDER BY qd
"""


def _price_stability(cur, synonyms: list[str]) -> tuple[float | None, float | None]:
    """Returns (stability_pct, avg_psqft).
    stability_pct = 100 - coefficient_of_variation, clamped 0-100."""
    cur.execute(SQL_VOLATILITY, (synonyms,))
    rows = cur.fetchall()
    if len(rows) < 3:
        return None, None
    vals = [float(r[1]) for r in rows if r[1]]
    if len(vals) < 3:
        return None, None
    mu = sum(vals) / len(vals)
    if mu <= 0:
        return None, None
    sd = statistics.pstdev(vals)
    cv = sd / mu * 100.0  # %
    stability = max(0.0, min(100.0, 100.0 - cv))
    return round(stability, 2), round(mu, 2)


def _score(m: dict) -> float:
    """Composite: schools/safety/amenities/family/walk on 0-10 → ×10; stability 0-100."""
    s = (m.get("schools_score") or 0) * 10
    sa = (m.get("safety_score") or 0) * 10
    a = (m.get("amenities_score") or 0) * 10
    f = (m.get("family_friendly_score") or 0) * 10
    w = (m.get("walk_score") or 0) * 10
    st = (m.get("price_stability_pct") or 0)
    return round(s * 0.25 + sa * 0.20 + a * 0.15 + f * 0.20 + w * 0.10 + st * 0.10, 2)


def run(dry_run: bool = False) -> int:
    t0 = time.time()
    raw: list[tuple[str, list[str], dict]] = []
    with get_dld_conn() as dld:
        with dld.cursor() as cur:
            for canon, synonyms in AREA_CATALOG.items():
                base = LIVING_CURATED.get(canon)
                if not base:
                    continue
                try:
                    stability, avg_psqft = _price_stability(cur, synonyms)
                except Exception as e:
                    log.warning("[living] %s SQL failed: %s", canon, e)
                    stability, avg_psqft = None, None
                m = {
                    "schools_score":         base["schools"],
                    "safety_score":          base["safety"],
                    "amenities_score":       base["amenities"],
                    "family_friendly_score": base["family"],
                    "walk_score":            base["walk"],
                    "price_stability_pct":   stability,
                    "avg_psqft":             avg_psqft,
                }
                raw.append((canon, synonyms, m))

    if not raw:
        log.error("[living] no curated areas")
        log_refresh("living", 0, "no_data")
        return 0

    scored = [(c, syn, m, _score(m)) for c, syn, m in raw]
    scored.sort(key=lambda x: x[3], reverse=True)

    if dry_run:
        for rank, (c, _, m, sc) in enumerate(scored, 1):
            log.info("[living dry] #%d %s score=%.1f stab=%s schools=%s family=%s",
                     rank, c, sc, m.get("price_stability_pct"),
                     m.get("schools_score"), m.get("family_friendly_score"))
        return len(scored)

    n = 0
    with get_intel_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for rank, (canon, syn, m, sc) in enumerate(scored, 1):
                upsert_ranking(cur, "living", canon, syn, rank, sc, m, "hybrid")
                n += 1
    log.info("[living] upserted %d rows in %.1fs", n, time.time() - t0)
    log_refresh("living", n, None)
    return n


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    dry = "--dry" in sys.argv
    run(dry_run=dry)
