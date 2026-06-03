"""liquidity_tier — A/B/C/D liquidity classification based on DLD velocity.

Computes average days between deals (or simple deals/month velocity) for a
building/area and classifies into 4 tiers:

  A — < 30 days avg (Burj Khalifa, Marina prime, Downtown prime)
  B — 30-90 days (Downtown secondary, JVC popular)
  C — 90-180 days (newer off-plan, secondary areas)
  D — 180+ days (illiquid)

If DLD data is unavailable, falls back to deals_count-based heuristic.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

log = logging.getLogger("pdf_intelligence.liquidity")

# tier color map (hex RGB)
TIER_COLORS = {
    "A": "#16A34A",   # green
    "B": "#1D4ED8",   # blue
    "C": "#CA8A04",   # yellow / amber
    "D": "#DC2626",   # red
}

TIER_LABELS_RU = {
    "A": "Высоколиквидно",
    "B": "Ликвидно",
    "C": "Умеренно",
    "D": "Низкая ликвидность",
}

TIER_LABELS_EN = {
    "A": "Highly liquid",
    "B": "Liquid",
    "C": "Moderate",
    "D": "Illiquid",
}


def tier_badge_color(tier: str) -> str:
    return TIER_COLORS.get(tier, "#888888")


def _classify_by_days(avg_days: float) -> str:
    if avg_days < 30:
        return "A"
    if avg_days < 90:
        return "B"
    if avg_days < 180:
        return "C"
    return "D"


def _classify_by_deals_per_month(deals_per_month: float) -> str:
    # >= 1 deal/month — A
    # 0.5-1 — B
    # 0.2-0.5 — C
    # <0.2 — D
    if deals_per_month >= 1.0:
        return "A"
    if deals_per_month >= 0.5:
        return "B"
    if deals_per_month >= 0.2:
        return "C"
    return "D"


def _db_url() -> Optional[str]:
    for k in ("DLD_DATABASE_URL", "ANALYTICS_DATABASE_URL",
              "INTELLIGENCE_DATABASE_URL", "DATABASE_URL"):
        v = os.environ.get(k)
        if v and "postgres" in v.lower():
            return v
    return None


def _fetch_dld_velocity(building: Optional[str], area: Optional[str]) -> Dict[str, Any]:
    """Query DLD archive to compute avg days between deals and 24-month deal count.

    Returns dict: {avg_days_between, deals_24m, source}.
    Falls back to {} if DB unavailable or no rows.
    """
    url = _db_url()
    if not url or (not building and not area):
        return {}
    try:
        import psycopg  # type: ignore
    except Exception:
        try:
            import psycopg2 as psycopg  # type: ignore
        except Exception:
            return {}

    sql_building = """
        WITH ordered AS (
          SELECT instance_date::date AS d
            FROM public.dld_sale_archive
           WHERE LOWER(building_name_en) = LOWER(%s)
             AND instance_date >= CURRENT_DATE - INTERVAL '24 months'
           ORDER BY instance_date
        ),
        gaps AS (
          SELECT (d - LAG(d) OVER (ORDER BY d))::int AS gap_days FROM ordered
        )
        SELECT COUNT(*) AS deals_24m,
               AVG(gap_days) AS avg_days_between
          FROM ordered, gaps
    """
    sql_area = """
        WITH ordered AS (
          SELECT instance_date::date AS d
            FROM public.dld_sale_archive
           WHERE LOWER(area_name_en) = LOWER(%s)
             AND instance_date >= CURRENT_DATE - INTERVAL '24 months'
           ORDER BY instance_date
        ),
        gaps AS (
          SELECT (d - LAG(d) OVER (ORDER BY d))::int AS gap_days FROM ordered
        )
        SELECT COUNT(*) AS deals_24m,
               AVG(gap_days) AS avg_days_between
          FROM ordered, gaps
    """

    try:
        with psycopg.connect(url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                if building:
                    cur.execute(sql_building, (building,))
                    row = cur.fetchone()
                    if row and row[0] and row[0] > 3:
                        return {
                            "deals_24m": int(row[0] or 0),
                            "avg_days_between": float(row[1] or 0),
                            "source": "dld_building",
                        }
                if area:
                    cur.execute(sql_area, (area,))
                    row = cur.fetchone()
                    if row and row[0] and row[0] > 10:
                        return {
                            "deals_24m": int(row[0] or 0),
                            "avg_days_between": float(row[1] or 0),
                            "source": "dld_area",
                        }
    except Exception as e:
        log.debug(f"DLD velocity query failed: {e}")
    return {}


def compute_liquidity_tier(building: Optional[str] = None,
                           area: Optional[str] = None,
                           deals_12m: Optional[int] = None,
                           lang: str = "ru") -> Dict[str, Any]:
    """Classify building/area liquidity into A/B/C/D tier.

    Strategy:
      1. Try DLD velocity (avg days between deals over 24 months).
      2. Fall back to deals_12m-based heuristic.

    Returns dict: {tier, color, label, avg_days_between, deals_24m, source}.
    """
    dld = _fetch_dld_velocity(building, area)
    labels = TIER_LABELS_RU if lang == "ru" else TIER_LABELS_EN

    if dld and dld.get("avg_days_between"):
        tier = _classify_by_days(dld["avg_days_between"])
        return {
            "tier": tier,
            "color": tier_badge_color(tier),
            "label": labels[tier],
            "avg_days_between": round(dld["avg_days_between"], 1),
            "deals_24m": dld["deals_24m"],
            "source": dld["source"],
        }

    # ── deals_12m fallback ────────────────────────────────────────────
    if deals_12m and deals_12m > 0:
        dpm = deals_12m / 12.0
        tier = _classify_by_deals_per_month(dpm)
        return {
            "tier": tier,
            "color": tier_badge_color(tier),
            "label": labels[tier],
            "avg_days_between": round(30.0 / max(dpm, 0.01), 1) if dpm > 0 else None,
            "deals_24m": deals_12m * 2,  # extrapolated
            "source": "deals_count_fallback",
        }

    # ── default: unknown ──────────────────────────────────────────────
    return {
        "tier": "C",
        "color": tier_badge_color("C"),
        "label": labels["C"],
        "avg_days_between": None,
        "deals_24m": None,
        "source": "unknown",
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for b, a in [("Marina Crown", "Dubai Marina"),
                  ("Address Opera", "Downtown Dubai"),
                  ("Grande", "Downtown Dubai"),
                  (None, "Jumeirah Village Circle")]:
        t = compute_liquidity_tier(building=b, area=a, deals_12m=120)
        print(f"{b or a:30s} → tier {t['tier']} ({t['label']}) source={t['source']}")
