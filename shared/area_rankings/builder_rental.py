# -*- coding: utf-8 -*-
"""builder_rental — rank Dubai areas for the 'Rental' goal.

Signals:
  - avg_rent_annual  = AVG annual_amount, last 12mo, dld_rents_full
  - avg_psqft_sales  = AVG meter_sale_price, last 12mo, dld_sales_unified
  - rental_yield_pct = avg_rent_annual / (avg_psqft_sales * avg_unit_sqft_proxy) * 100
    Proxy: we approximate per-unit price via avg actual_worth on same area
  - rent volume = COUNT rents 12mo
  - vacancy_score = recent rent volume / 12mo rent volume * 100 (higher = healthier turnover)
  - growth = 12mo change in avg rent

Score = weighted(yield=60%, vacancy=20%, growth=20%).
"""
from __future__ import annotations
import logging
import time

from ._common import (
    AREA_CATALOG, get_dld_conn, get_intel_conn, upsert_ranking, log_refresh,
)

log = logging.getLogger("area_rankings.rental")

SQL_RENT = """
-- 2026-06-13: dld_rent_archive deprecated (0 rows post DLD→Supabase consolidation).
-- Repointed to live dld_rents_full (Pulse schema: area_en/start_date, numeric cols).
WITH r AS (
  SELECT annual_amount::numeric AS rent,
         actual_area::numeric    AS sqft,
         start_date::date         AS d
  FROM public.dld_rents_full
  WHERE area_en = ANY(%s)
    AND annual_amount BETWEEN 10000 AND 20000000
    AND actual_area > 20
    AND start_date::date >= CURRENT_DATE - INTERVAL '24 months'
    AND start_date::date <= CURRENT_DATE
)
SELECT
  AVG(rent) FILTER (WHERE d >= CURRENT_DATE - INTERVAL '12 months')                                    AS rent_now,
  AVG(rent) FILTER (WHERE d >= CURRENT_DATE - INTERVAL '24 months' AND d < CURRENT_DATE - INTERVAL '12 months') AS rent_then,
  COUNT(*)  FILTER (WHERE d >= CURRENT_DATE - INTERVAL '12 months')                                    AS vol_12,
  COUNT(*)  FILTER (WHERE d >= CURRENT_DATE - INTERVAL '3 months')                                     AS vol_3
FROM r
WHERE rent > 0
"""

SQL_PRICE = """
-- 2026-06-13: dld_sales_unified absent on RESALE_DATABASE_URL; use dld_sale_archive
-- (1.7M rows, text cols guarded by numeric-regex).
SELECT AVG(NULLIF(actual_worth,'')::numeric) AS avg_price,
       AVG(NULLIF(meter_sale_price,'')::numeric) AS avg_psqft
FROM public.dld_sale_archive
WHERE area_name_en = ANY(%s)
  AND actual_worth ~ '^[0-9]+\\.?[0-9]*$'
  AND meter_sale_price ~ '^[0-9]+\\.?[0-9]*$'
  AND COALESCE(procedure_name_en,'') NOT ILIKE '%%mortgage%%'
  AND instance_date::date >= (CURRENT_DATE - INTERVAL '12 months')
"""


def _compute_for_area(rent_cur, sale_cur, synonyms: list[str]) -> dict | None:
    rent_cur.execute(SQL_RENT, (synonyms,))
    rent_row = rent_cur.fetchone()
    if not rent_row:
        return None
    rent_now, rent_then, vol_12, vol_3 = rent_row
    if not vol_12 or vol_12 < 30:
        return None

    sale_cur.execute(SQL_PRICE, (synonyms,))
    sale_row = sale_cur.fetchone()
    avg_price, _avg_psqft = (sale_row or (None, None))
    if not avg_price or float(avg_price) <= 0:
        return None

    yield_pct = float(rent_now) / float(avg_price) * 100.0 if rent_now else None
    rent_growth = None
    if rent_now and rent_then and float(rent_then) > 0:
        rent_growth = float(rent_now - rent_then) / float(rent_then) * 100.0
    vacancy = (float(vol_3 or 0) / float(vol_12) * 100.0) if vol_12 else None

    return {
        "rental_yield_pct":      round(yield_pct, 2) if yield_pct else None,
        "avg_rent_annual":       round(float(rent_now), 0) if rent_now else None,
        "vacancy_score":         round(vacancy, 2) if vacancy else None,
        "price_growth_12mo_pct": round(rent_growth, 2) if rent_growth is not None else None,
        "avg_psqft":             round(float(_avg_psqft), 2) if _avg_psqft else None,
        "deal_volume_12mo":      int(vol_12),
    }


def _score(m: dict, max_yield: float, max_vac: float, max_growth: float) -> float:
    y = m.get("rental_yield_pct") or 0.0
    v = m.get("vacancy_score") or 0.0
    g = max(0.0, m.get("price_growth_12mo_pct") or 0.0)
    y_n = (y / max_yield * 100.0) if max_yield > 0 else 0.0
    v_n = (v / max_vac * 100.0) if max_vac > 0 else 0.0
    g_n = (g / max_growth * 100.0) if max_growth > 0 else 0.0
    return round(y_n * 0.60 + v_n * 0.20 + g_n * 0.20, 2)


def run(dry_run: bool = False) -> int:
    t0 = time.time()
    raw: list[tuple[str, list[str], dict]] = []
    with get_dld_conn() as dld:
        with dld.cursor() as rc, dld.cursor() as sc:
            for canon, synonyms in AREA_CATALOG.items():
                try:
                    m = _compute_for_area(rc, sc, synonyms)
                except Exception as e:
                    log.warning("[rental] %s SQL failed: %s", canon, e)
                    continue
                if not m:
                    continue
                raw.append((canon, synonyms, m))

    if not raw:
        log.error("[rental] no areas with data")
        log_refresh("rental", 0, "no_data")
        return 0

    max_yield = max([(m.get("rental_yield_pct") or 0.0) for _, _, m in raw] + [3.0])
    max_vac   = max([(m.get("vacancy_score") or 0.0) for _, _, m in raw] + [1.0])
    max_grow  = max([(m.get("price_growth_12mo_pct") or 0.0) for _, _, m in raw] + [5.0])

    scored = [(c, syn, m, _score(m, max_yield, max_vac, max_grow))
              for c, syn, m in raw]
    scored.sort(key=lambda x: x[3], reverse=True)

    if dry_run:
        for rank, (c, _, m, sc) in enumerate(scored, 1):
            log.info("[rental dry] #%d %s score=%.1f yield=%s vol=%s rent=%s",
                     rank, c, sc, m.get("rental_yield_pct"),
                     m.get("deal_volume_12mo"), m.get("avg_rent_annual"))
        return len(scored)

    n = 0
    with get_intel_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for rank, (canon, syn, m, sc) in enumerate(scored, 1):
                upsert_ranking(cur, "rental", canon, syn, rank, sc, m, "dld_recent")
                n += 1
    log.info("[rental] upserted %d rows in %.1fs", n, time.time() - t0)
    log_refresh("rental", n, None)
    return n


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    dry = "--dry" in sys.argv
    run(dry_run=dry)
