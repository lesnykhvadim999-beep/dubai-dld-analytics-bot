# -*- coding: utf-8 -*-
"""
area_search.py — единый канонический поиск районов для всех ботов.

Правило (feedback_bots_must_be_fast.md):
    Боты ОБЯЗАНЫ ходить в сгруппированные таблицы master_project_zones / area_stats.
    Не использовать raw dld_* для поиска.

Каскад из 4 шагов (по убыванию приоритета):
    1. master-zone exact match: query == master_zone | area_key | display_en | display_ru
    2. master-zone fuzzy:       master_zone / display_en / display_ru ILIKE %q%
    3. sub-area → master-zone:  query совпадает с элементом dld_master_project_names
    4. area_stats fuzzy:        обычный DLD-район БЕЗ группы (не в любом master_zone)

Результат: список dict с полями kind, name, master_zone, area_key, display_ru,
display_en, deals_12m, hint (если sub-area матч — пометка «вы выбрали часть JVC»).

Использование:
    from area_search import search_area_canonical
    results = search_area_canonical("JVC")        # → 1 master_zone
    results = search_area_canonical("Marsa Dubai") # → master_zone Dubai Marina (с hint)
"""

from __future__ import annotations

import os
import re
import time
import threading
import logging
import psycopg2
import psycopg2.extras
from typing import List, Dict, Any, Optional

LOG = logging.getLogger("area_search")

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

_LOCAL = threading.local()


def _resolve_url() -> Optional[str]:
    return (
        os.environ.get("READ_MODEL_DATABASE_URL")
        or os.environ.get("ANALYTICS_DATABASE_URL")
        or os.environ.get("ARCHIVE_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
    )


def _conn():
    url = _resolve_url()
    if not url:
        raise RuntimeError("area_search: no DATABASE_URL configured")
    c = getattr(_LOCAL, "conn", None)
    last = getattr(_LOCAL, "ping", 0.0)
    now = time.time()
    if c is not None:
        if (now - last) < 60:
            return c
        try:
            with c.cursor() as cur:
                cur.execute("SELECT 1")
            _LOCAL.ping = now
            return c
        except Exception:
            try: c.close()
            except Exception: pass
            _LOCAL.conn = None
    c = psycopg2.connect(url, connect_timeout=5, application_name="area_search")
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("SET statement_timeout = 4000")  # 4s read-only cap
    _LOCAL.conn = c
    _LOCAL.ping = now
    return c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def _clean(q: str) -> str:
    if not q:
        return ""
    q = q.strip().lower()
    q = _WS_RE.sub(" ", q)
    # отрезаем эмодзи / лишние символы
    q = re.sub(r"[^\w \-Ѐ-ӿ؀-ۿ()]", " ", q)
    q = _WS_RE.sub(" ", q).strip()
    return q


_DEALS_CTE = """
WITH master_deals AS (
    SELECT mpz.master_zone,
           SUM(s.deals_count) AS deals_12m,
           AVG(s.avg_rental_yield_pct) AS yield_avg
    FROM master_project_zones mpz
    LEFT JOIN area_stats s
      ON (
           lower(s.area_name) = lower(mpz.master_zone)
        OR lower(s.area_name) = ANY (SELECT lower(unnest(mpz.dld_master_project_names)))
        OR lower(s.area_key)  = lower(mpz.area_key)
      )
     AND s.deal_type = 'sale'
     AND s.period_month >= NOW() - INTERVAL '12 months'
    GROUP BY mpz.master_zone
)
"""


def _row_master(r, hint: Optional[str] = None) -> Dict[str, Any]:
    return {
        "kind":         "master_zone",
        "name":         r["master_zone"],
        "master_zone":  r["master_zone"],
        "area_key":     r.get("area_key"),
        "display_en":   r.get("display_en") or r["master_zone"],
        "display_ru":   r.get("display_ru") or r["master_zone"],
        "deals_12m":    int(r["deals_12m"] or 0) if r.get("deals_12m") is not None else None,
        "yield_avg":    float(r["yield_avg"]) if r.get("yield_avg") is not None else None,
        "hint":         hint,
    }


def _row_area(r) -> Dict[str, Any]:
    return {
        "kind":         "area",
        "name":         r["area_name"],
        "master_zone":  None,
        "area_key":     r.get("area_key"),
        "display_en":   r["area_name"],
        "display_ru":   r["area_name"],
        "deals_12m":    int(r["deals_12m"] or 0) if r.get("deals_12m") is not None else None,
        "yield_avg":    None,
        "hint":         None,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_area_canonical(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Канонический поиск района. Возвращает <=`limit` уникальных матчей.

    Master-zones всегда приоритетнее обычных DLD area. Дубликаты по master_zone
    отсекаются на каждом шаге.
    """
    q = _clean(query)
    if not q:
        return []

    out: List[Dict[str, Any]] = []
    seen_master: set = set()
    seen_area: set = set()
    started = time.time()

    try:
        with _conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # ---------- 1. master-zone exact ----------
            cur.execute(_DEALS_CTE + """
                SELECT mpz.master_zone, mpz.area_key, mpz.display_en, mpz.display_ru,
                       md.deals_12m, md.yield_avg
                FROM master_project_zones mpz
                LEFT JOIN master_deals md ON md.master_zone = mpz.master_zone
                WHERE lower(mpz.master_zone) = %s
                   OR lower(mpz.area_key) = %s
                   OR lower(COALESCE(mpz.display_en, '')) = %s
                   OR lower(COALESCE(mpz.display_ru, '')) = %s
                ORDER BY md.deals_12m DESC NULLS LAST
                LIMIT %s
            """, (q, q, q, q, limit))
            for r in cur.fetchall():
                if r["master_zone"] in seen_master:
                    continue
                seen_master.add(r["master_zone"])
                out.append(_row_master(r))

            if len(out) >= limit:
                return out[:limit]

            # ---------- 3. sub-area → master (раньше fuzzy, т.к. это точный mapping) ----------
            cur.execute(_DEALS_CTE + """
                SELECT mpz.master_zone, mpz.area_key, mpz.display_en, mpz.display_ru,
                       md.deals_12m, md.yield_avg,
                       (SELECT dn FROM unnest(mpz.dld_master_project_names) AS dn
                         WHERE lower(dn) = %s OR lower(dn) ILIKE %s LIMIT 1) AS matched_dld_name
                FROM master_project_zones mpz
                LEFT JOIN master_deals md ON md.master_zone = mpz.master_zone
                WHERE %s = ANY (SELECT lower(unnest(mpz.dld_master_project_names)))
                   OR EXISTS (
                        SELECT 1 FROM unnest(mpz.dld_master_project_names) AS dn
                        WHERE lower(dn) ILIKE %s
                   )
                ORDER BY md.deals_12m DESC NULLS LAST
                LIMIT %s
            """, (q, f"%{q}%", q, f"%{q}%", limit))
            for r in cur.fetchall():
                if r["master_zone"] in seen_master:
                    continue
                seen_master.add(r["master_zone"])
                hint = None
                if r.get("matched_dld_name") and q != r["master_zone"].lower():
                    hint = f"вы выбрали часть {r['master_zone']} ({r['matched_dld_name']})"
                out.append(_row_master(r, hint=hint))

            if len(out) >= limit:
                return out[:limit]

            # ---------- 2. master-zone fuzzy ----------
            like = f"%{q}%"
            cur.execute(_DEALS_CTE + """
                SELECT mpz.master_zone, mpz.area_key, mpz.display_en, mpz.display_ru,
                       md.deals_12m, md.yield_avg
                FROM master_project_zones mpz
                LEFT JOIN master_deals md ON md.master_zone = mpz.master_zone
                WHERE mpz.master_zone ILIKE %s
                   OR COALESCE(mpz.display_en, '') ILIKE %s
                   OR COALESCE(mpz.display_ru, '') ILIKE %s
                   OR mpz.area_key ILIKE %s
                ORDER BY md.deals_12m DESC NULLS LAST
                LIMIT %s
            """, (like, like, like, like, limit))
            for r in cur.fetchall():
                if r["master_zone"] in seen_master:
                    continue
                seen_master.add(r["master_zone"])
                out.append(_row_master(r))

            if len(out) >= limit:
                return out[:limit]

            # ---------- 4. area_stats fuzzy (DLD without master) ----------
            cur.execute("""
                SELECT s.area_name, s.area_key,
                       SUM(s.deals_count) AS deals_12m
                FROM area_stats s
                WHERE s.deal_type = 'sale'
                  AND s.period_month >= NOW() - INTERVAL '12 months'
                  AND s.area_name ILIKE %s
                  AND NOT EXISTS (
                        SELECT 1 FROM master_project_zones mpz
                        WHERE lower(mpz.master_zone) = lower(s.area_name)
                           OR lower(s.area_name) = ANY (SELECT lower(unnest(mpz.dld_master_project_names)))
                  )
                GROUP BY s.area_name, s.area_key
                ORDER BY deals_12m DESC NULLS LAST
                LIMIT %s
            """, (like, limit))
            for r in cur.fetchall():
                key = (r["area_name"] or "").lower()
                if key in seen_area:
                    continue
                seen_area.add(key)
                out.append(_row_area(r))
    except Exception as e:
        LOG.warning("search_area_canonical err: %r", e)
        return out
    finally:
        dt = (time.time() - started) * 1000
        if dt > 500:
            LOG.warning("[LAT_SLOW] search_area_canonical %.0fms q=%r", dt, query)

    return out[:limit]


def list_master_zones() -> List[Dict[str, Any]]:
    """Все 13 master-zones с deals_12m для UI-выбора (например, кнопки)."""
    try:
        with _conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_DEALS_CTE + """
                SELECT mpz.master_zone, mpz.area_key, mpz.display_en, mpz.display_ru,
                       md.deals_12m, md.yield_avg
                FROM master_project_zones mpz
                LEFT JOIN master_deals md ON md.master_zone = mpz.master_zone
                ORDER BY md.deals_12m DESC NULLS LAST
            """)
            return [_row_master(r) for r in cur.fetchall()]
    except Exception as e:
        LOG.warning("list_master_zones err: %r", e)
        return []


def resolve_to_master(query: str) -> Optional[Dict[str, Any]]:
    """Если query можно свести к master-zone — возвращает её dict. Иначе None."""
    rs = search_area_canonical(query, limit=1)
    if rs and rs[0]["kind"] == "master_zone":
        return rs[0]
    return None


if __name__ == "__main__":
    import json
    for q in ["JVC", "Marina", "Al Hebiah First", "Burj Khalifa",
              "Jumeirah Village Circle", "Marsa Dubai", "Downtown",
              "ARJAN", "Sobha", "MBR"]:
        t = time.time()
        res = search_area_canonical(q, limit=5)
        dt = (time.time() - t) * 1000
        print(f"{q!r}  ({dt:.0f}ms)  →  {len(res)} matches")
        for r in res:
            print("   ", json.dumps({k: v for k, v in r.items() if v is not None}, ensure_ascii=False))
