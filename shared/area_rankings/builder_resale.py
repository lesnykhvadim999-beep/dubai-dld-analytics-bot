# -*- coding: utf-8 -*-
"""builder_resale — rank Dubai areas for the 'Resale' goal.

Signals from dld_sales_unified:
  - price_growth_12mo_pct = (avg meter_sale_price last 3mo) vs (avg 12-15mo back)
  - avg_psqft = recent 12mo avg AED/sqft
  - deal_volume_12mo = COUNT last 12mo
  - liquidity_score = deal_volume_3mo / deal_volume_12mo * 100 (higher = more liquid recently)

Score = weighted(growth=40%, volume=30%, liquidity=30%) → normalized 0-100.
"""
from __future__ import annotations
import logging
import time

from ._common import (
    AREA_CATALOG, get_dld_conn, get_intel_conn, upsert_ranking, log_refresh,
)

log = logging.getLogger("area_rankings.resale")

# instance_date is TEXT in dld_sales_unified — but stored as ISO 'YYYY-MM-DD'.
# Parse via TO_DATE(NULLIF(instance_date,''), 'YYYY-MM-DD') with fallback.
SQL = """
WITH s AS (
  SELECT
    NULLIF(meter_sale_price, '')::numeric           AS psqft,
    NULLIF(actual_worth, '')::numeric               AS price,
    TO_DATE(NULLIF(instance_date,''),'YYYY-MM-DD')  AS d
  FROM public.dld_sales_unified
  WHERE area_name_en = ANY(%s)
    AND COALESCE(procedure_name_en,'') NOT ILIKE '%%mortgage%%'
    AND NULLIF(meter_sale_price,'') IS NOT NULL
    AND NULLIF(instance_date,'') IS NOT NULL
)
SELECT
  AVG(psqft) FILTER (WHERE d >= (CURRENT_DATE - INTERVAL '3 months'))            AS psqft_now,
  AVG(psqft) FILTER (WHERE d >= (CURRENT_DATE - INTERVAL '15 months')
                       AND d <  (CURRENT_DATE - INTERVAL '12 months'))           AS psqft_then,
  COUNT(*)   FILTER (WHERE d >= (CURRENT_DATE - INTERVAL '12 months'))           AS vol_12mo,
  COUNT(*)   FILTER (WHERE d >= (CURRENT_DATE - INTERVAL '3 months'))            AS vol_3mo
FROM s
"""


def _compute_for_area(cur, synonyms: list[str]) -> dict | None:
    cur.execute(SQL, (synonyms,))
    row = cur.fetchone()
    if not row:
        return None
    psqft_now, psqft_then, vol_12, vol_3 = row
    if not vol_12 or vol_12 < 30:  # too thin to rank
        return None
    growth = None
    if psqft_now and psqft_then and float(psqft_then) > 0:
        growth = float(psqft_now - psqft_then) / float(psqft_then) * 100.0
    liquidity = None
    if vol_12:
        liquidity = float(vol_3 or 0) / float(vol_12) * 100.0
    return {
        "price_growth_12mo_pct": round(growth, 2) if growth is not None else None,
        "avg_psqft":             round(float(psqft_now), 2) if psqft_now else None,
        "deal_volume_12mo":      int(vol_12),
        "liquidity_score":       round(liquidity, 2) if liquidity is not None else None,
    }


def _score(m: dict, max_vol: int, max_growth: float, max_liq: float) -> float:
    """Normalize each signal to 0-100, weighted blend. Missing → 0 contrib."""
    g = m.get("price_growth_12mo_pct") or 0.0
    v = m.get("deal_volume_12mo") or 0
    l = m.get("liquidity_score") or 0.0
    g_n = max(0.0, min(100.0, (g / max_growth * 100.0) if max_growth > 0 else 0.0))
    v_n = (v / max_vol * 100.0) if max_vol > 0 else 0.0
    l_n = (l / max_liq * 100.0) if max_liq > 0 else 0.0
    return round(g_n * 0.40 + v_n * 0.30 + l_n * 0.30, 2)


def run(dry_run: bool = False) -> int:
    """Refresh resale rankings. Returns # rows written."""
    t0 = time.time()
    raw: list[tuple[str, list[str], dict]] = []
    with get_dld_conn() as dld:
        with dld.cursor() as cur:
            for canon, synonyms in AREA_CATALOG.items():
                try:
                    m = _compute_for_area(cur, synonyms)
                except Exception as e:
                    log.warning("[resale] %s SQL failed: %s", canon, e)
                    continue
                if not m:
                    continue
                raw.append((canon, synonyms, m))

    if not raw:
        log.error("[resale] no areas with data")
        log_refresh("resale", 0, "no_data")
        return 0

    max_vol = max((m["deal_volume_12mo"] or 0) for _, _, m in raw) or 1
    growths = [(m["price_growth_12mo_pct"] or 0.0) for _, _, m in raw]
    max_growth = max([g for g in growths if g > 0] + [10.0])  # floor to avoid /0
    max_liq = max([(m["liquidity_score"] or 0.0) for _, _, m in raw] + [1.0])

    scored = [(c, syn, m, _score(m, max_vol, max_growth, max_liq))
              for c, syn, m in raw]
    scored.sort(key=lambda x: x[3], reverse=True)

    if dry_run:
        for rank, (c, _, m, sc) in enumerate(scored, 1):
            log.info("[resale dry] #%d %s score=%.1f growth=%s vol=%s liq=%s",
                     rank, c, sc, m.get("price_growth_12mo_pct"),
                     m.get("deal_volume_12mo"), m.get("liquidity_score"))
        return len(scored)

    n = 0
    with get_intel_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for rank, (canon, syn, m, sc) in enumerate(scored, 1):
                upsert_ranking(cur, "resale", canon, syn, rank, sc, m, "dld_recent")
                n += 1
    log.info("[resale] upserted %d rows in %.1fs", n, time.time() - t0)
    log_refresh("resale", n, None)
    return n


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    dry = "--dry" in sys.argv
    run(dry_run=dry)
