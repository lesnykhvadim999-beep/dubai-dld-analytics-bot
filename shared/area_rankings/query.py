# -*- coding: utf-8 -*-
"""Read API for analytics-bot and other consumers."""
from __future__ import annotations
import logging
from typing import Optional

from ._common import get_intel_conn

log = logging.getLogger("area_rankings.query")


def query_area_rankings_top(goal: str, limit: int = 8) -> list[dict]:
    """Return top-N areas for a goal, ordered by rank ASC. Empty list on error."""
    try:
        with get_intel_conn(timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT area_name, area_synonyms, rank, score,
                              price_growth_12mo_pct, avg_psqft, deal_volume_12mo,
                              liquidity_score, rental_yield_pct, avg_rent_annual,
                              vacancy_score, schools_score, safety_score,
                              amenities_score, family_friendly_score, walk_score,
                              price_stability_pct, updated_at
                       FROM area_rankings
                       WHERE goal = %s
                       ORDER BY rank ASC NULLS LAST
                       LIMIT %s""",
                    (goal, limit),
                )
                cols = [c.name for c in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        log.warning("[query] failed for goal=%s: %s", goal, e)
        return []


def last_refresh_at(goal: Optional[str] = None) -> Optional[str]:
    """Return ISO timestamp of most recent successful refresh."""
    try:
        with get_intel_conn(timeout=5) as conn:
            with conn.cursor() as cur:
                if goal:
                    cur.execute(
                        """SELECT MAX(finished_at) FROM area_rankings_refresh_log
                           WHERE goal=%s AND error IS NULL""", (goal,))
                else:
                    cur.execute(
                        """SELECT MAX(finished_at) FROM area_rankings_refresh_log
                           WHERE error IS NULL""")
                row = cur.fetchone()
                return row[0].isoformat() if row and row[0] else None
    except Exception:
        return None


# i18n labels for "Top for ..." UI block
TOP_LABELS = {
    "resale": {"ru": "Топ для перепродажи (DLD)",
               "en": "Top for resale (DLD)",
               "ar": "الأفضل لإعادة البيع (DLD)"},
    "rental": {"ru": "Топ для аренды (DLD)",
               "en": "Top for rental (DLD)",
               "ar": "الأفضل للإيجار (DLD)"},
    "living": {"ru": "Топ для жизни (гибрид)",
               "en": "Top for living (hybrid)",
               "ar": "الأفضل للسكن (هجين)"},
}


def format_why(goal: str, row: dict, lang: str = "ru") -> str:
    """Build short 'why this area' explanation for a single ranking row."""
    if goal == "resale":
        g = row.get("price_growth_12mo_pct")
        v = row.get("deal_volume_12mo")
        bits = []
        if g is not None:
            bits.append(f"рост {g:+.1f}%" if lang == "ru" else
                        f"نمو {g:+.1f}%" if lang == "ar" else
                        f"growth {g:+.1f}%")
        if v:
            bits.append(f"{int(v):,} сделок" if lang == "ru" else
                        f"{int(v):,} صفقة" if lang == "ar" else
                        f"{int(v):,} deals")
        return ", ".join(bits)
    if goal == "rental":
        y = row.get("rental_yield_pct")
        r = row.get("avg_rent_annual")
        bits = []
        if y is not None:
            bits.append(f"yield {y:.1f}%")
        if r:
            bits.append(f"~{int(r):,} AED/год" if lang == "ru" else
                        f"~{int(r):,} AED/سنوي" if lang == "ar" else
                        f"~{int(r):,} AED/yr")
        return ", ".join(bits)
    if goal == "living":
        s = row.get("schools_score")
        f = row.get("family_friendly_score")
        bits = []
        if s is not None:
            bits.append(f"школы {s:.0f}/10" if lang == "ru" else
                        f"مدارس {s:.0f}/10" if lang == "ar" else
                        f"schools {s:.0f}/10")
        if f is not None:
            bits.append(f"семья {f:.0f}/10" if lang == "ru" else
                        f"عائلي {f:.0f}/10" if lang == "ar" else
                        f"family {f:.0f}/10")
        return ", ".join(bits)
    return ""
