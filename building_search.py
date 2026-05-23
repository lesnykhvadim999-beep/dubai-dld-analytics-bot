# -*- coding: utf-8 -*-
"""
building_search.py — единый канонический fuzzy-поиск зданий для всех 4 ботов.

Цель: пользователь НИКОГДА не получает пустой ответ.
Если точного матча нет — отдаём похожие здания с пометкой kind="fuzzy"/"prefix"/...,
плюс master-zone привязку (из master_project_zones).

Каскад из 5 уровней (ранжирование по убыванию приоритета):
    1. EXACT      — lower(building_name_display) == q
    2. PREFIX     — lower(building_name_display) LIKE 'q%'
    3. SUBSTRING  — lower(building_name_display) ILIKE '%q%'
    4. TRIGRAM    — similarity >= 0.3 (pg_trgm, для опечаток)
    5. POPULAR    — top-N зданий по deals_12m (если всё пусто)

Также:
- BUILDING_ALIASES — сокращения ("khalifa" → "Burj Khalifa", "address opera" → "Address Residences Dubai Opera")
- Каждый результат содержит master_zone (если здание принадлежит master-zone)

Использование:
    from building_search import search_building_canonical
    results = search_building_canonical("burj k")            # → Burj Khalifa
    results = search_building_canonical("marina pinacle")    # → Marina Pinnacle Tower (опечатка)
    results = search_building_canonical("xyzabc")            # → top-5 популярных

Возвращает: List[Dict] с полями:
    name             - канонический building_name_display (как в архиве)
    name_lower       - lowercase копия для поиска
    area_name        - DLD area_name
    master_zone      - связанный master-zone (через master_project_zones), либо None
    deals_12m        - суммарное число сделок за 12 месяцев
    avg_price        - средняя цена сделки (если есть)
    avg_price_psf    - средняя цена за sqft
    similarity       - 0..1 для kind=fuzzy, иначе None
    kind             - exact | prefix | substring | fuzzy | popular
"""

from __future__ import annotations

import os
import re
import time
import threading
import logging
import psycopg2
import psycopg2.extras
from typing import List, Dict, Any, Optional, Iterable

LOG = logging.getLogger("building_search")

# ---------------------------------------------------------------------------
# Connection (повторяем паттерн area_search.py)
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
        raise RuntimeError("building_search: no DATABASE_URL configured")
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
    c = psycopg2.connect(url, connect_timeout=5, application_name="building_search")
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("SET statement_timeout = 4000")  # 4s cap
        # Снижаем порог триграмм для опечаток
        try:
            cur.execute("SET pg_trgm.similarity_threshold = 0.3")
        except Exception:
            pass
    _LOCAL.conn = c
    _LOCAL.ping = now
    return c


# ---------------------------------------------------------------------------
# Aliases — сокращения и нестандартные написания
# ---------------------------------------------------------------------------
# Ключи — lowercase user input. Значения — список канонических query-строк
# которые надо попробовать ВМЕСТО оригинала (по очереди).
# Если матча по канонической строке тоже нет — продолжаем стандартный каскад
# с оригинальным запросом.

BUILDING_ALIASES: Dict[str, List[str]] = {
    # Burj Khalifa
    "khalifa":          ["Burj Khalifa"],
    "burj k":           ["Burj Khalifa"],
    "burj khalif":      ["Burj Khalifa"],
    "халифа":           ["Burj Khalifa"],
    "бурдж халифа":     ["Burj Khalifa"],

    # Address (часто пишут с опечатками)
    "address opera":          ["Address Residences Dubai Opera", "Address Opera"],
    "address residences":     ["Address Residences"],
    "address boulevard":      ["The Address Boulevard"],
    "address downtown":       ["The Address Downtown"],
    "address dubai":          ["The Address Residences", "Address Residences Dubai Opera"],
    "address sky":            ["Address Sky View Tower", "Address Sky View"],
    "address skyview":        ["Address Sky View Tower"],
    "address fountain":       ["Address Fountain Views"],

    # Marina Pinnacle (часто с опечатками)
    "marina pinacle":         ["Marina Pinnacle"],
    "marina pinaccle":        ["Marina Pinnacle"],
    "marina pinnacle":        ["Marina Pinnacle"],
    "pinacle":                ["Marina Pinnacle", "The Pinnacle"],
    "pinnacle marina":        ["Marina Pinnacle"],

    # Marina Gate
    "marina gate":            ["Marina Gate"],

    # Armani / Versace
    "armani":                 ["Armani Residence", "Armani Hotel"],
    "versace":                ["Palazzo Versace"],

    # Sobha / Damac / Emaar headliners
    "sobha hartland":         ["Sobha Hartland"],
    "sobha one":              ["Sobha One"],
    "sobha creek":            ["Sobha Creek Vistas"],
    "damac hills":            ["DAMAC Hills"],
    "creek harbour":          ["Creek Harbour", "Dubai Creek Harbour"],
    "creek harbor":           ["Dubai Creek Harbour"],

    # Princess / Marina Star / 23 Marina
    "princess tower":         ["Princess Tower"],
    "23 marina":              ["23 Marina"],
    "marina star":            ["Marina Star"],
    "marina crown":           ["Marina Crown"],
    "marina diamond":         ["Marina Diamond"],
    "marina heights":         ["Marina Heights"],

    # Binghatti — часто общий запрос
    "binghatti":              ["Binghatti"],
    "бингхатти":              ["Binghatti"],

    # JBR
    "jbr":                    ["Jumeirah Beach Residence", "JBR"],
    "shams":                  ["Shams"],
    "amwaj":                  ["Amwaj"],
    "rimal":                  ["Rimal"],
    "murjan":                 ["Murjan"],
    "bahar":                  ["Bahar"],
    "sadaf":                  ["Sadaf"],

    # Grande / Opera / Boulevard
    "grande":                 ["Grande", "IL Primo"],
    "opera grand":            ["Opera Grand"],
    "il primo":               ["IL Primo"],
    "boulevard point":        ["Boulevard Point"],
    "boulevard heights":      ["Boulevard Heights"],

    # Park Towers / Vida
    "vida":                   ["Vida Residence"],
    "park towers":            ["Park Towers"],

    # Palm Jumeirah
    "atlantis":               ["Atlantis The Royal"],
    "atlantis royal":         ["Atlantis The Royal"],
    "the royal atlantis":     ["Atlantis The Royal"],
    "palm tower":             ["The Palm Tower"],
    "palm jumeirah":          ["Palm Jumeirah"],

    # русские транслитерации частые
    "марина":                 ["Marina"],
    "даунтаун":               ["Downtown"],
}


def _expand_aliases(q: str) -> List[str]:
    """Возвращает список вариантов запроса (включая оригинал).

    Сначала канонические значения из BUILDING_ALIASES (если ключ совпал),
    потом оригинальный запрос (чтобы каскад всё равно сработал).
    """
    q_low = q.lower().strip()
    out: List[str] = []
    if q_low in BUILDING_ALIASES:
        for v in BUILDING_ALIASES[q_low]:
            if v.lower() not in [x.lower() for x in out]:
                out.append(v)
    if q_low not in [x.lower() for x in out]:
        out.append(q)
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def _clean(q: str) -> str:
    if not q:
        return ""
    q = q.strip().lower()
    q = _WS_RE.sub(" ", q)
    q = re.sub(r"[^\w \-Ѐ-ӿ؀-ۿ()]", " ", q)
    q = _WS_RE.sub(" ", q).strip()
    return q


def _wrap_row(r: Dict[str, Any], kind: str, master_zone: Optional[str] = None) -> Dict[str, Any]:
    name = r.get("building_name_display") or ""
    return {
        "kind":          kind,
        "name":          name,
        "name_lower":    name.lower(),
        "area_name":     r.get("area_name"),
        "master_zone":   master_zone,
        "deals_12m":     int(r["deals"] or 0) if r.get("deals") is not None else None,
        "avg_price":     float(r["avg_price"]) if r.get("avg_price") is not None else None,
        "avg_price_psf": float(r["avg_price_psf"]) if r.get("avg_price_psf") is not None else None,
        "similarity":    float(r["sim"]) if r.get("sim") is not None else None,
    }


def _master_zone_index(cur, areas: Iterable[str]) -> Dict[str, str]:
    """Для списка area_name возвращает map {area_lower -> master_zone}."""
    areas_clean = sorted({(a or "").lower().strip() for a in areas if a})
    if not areas_clean:
        return {}
    try:
        cur.execute("""
            SELECT lower(unnest(mpz.dld_master_project_names)) AS dld_name,
                   mpz.master_zone
            FROM master_project_zones mpz
            UNION ALL
            SELECT lower(mpz.master_zone), mpz.master_zone
            FROM master_project_zones mpz
            UNION ALL
            SELECT lower(mpz.area_key), mpz.master_zone
            FROM master_project_zones mpz
        """)
        all_map: Dict[str, str] = {}
        for dld_name, master in cur.fetchall():
            if dld_name and master and dld_name not in all_map:
                all_map[dld_name] = master
        return {a: all_map[a] for a in areas_clean if a in all_map}
    except Exception as e:
        LOG.warning("_master_zone_index err: %r", e)
        return {}


# ---------------------------------------------------------------------------
# Cascade SQL
# ---------------------------------------------------------------------------

_SQL_EXACT = """
    SELECT building_name_display, area_name,
           SUM(deals) AS deals,
           AVG(avg_price) AS avg_price,
           AVG(avg_price_psf) AS avg_price_psf
    FROM mv_building_12m_summary
    WHERE deal_type='sale' AND rooms='all'
      AND lower(trim(building_name_display)) = %s
    GROUP BY building_name_display, area_name
    ORDER BY deals DESC NULLS LAST
    LIMIT %s
"""

_SQL_PREFIX = """
    SELECT building_name_display, area_name,
           SUM(deals) AS deals,
           AVG(avg_price) AS avg_price,
           AVG(avg_price_psf) AS avg_price_psf
    FROM mv_building_12m_summary
    WHERE deal_type='sale' AND rooms='all'
      AND lower(building_name_display) LIKE %s
    GROUP BY building_name_display, area_name
    ORDER BY deals DESC NULLS LAST
    LIMIT %s
"""

_SQL_SUBSTR = """
    SELECT building_name_display, area_name,
           SUM(deals) AS deals,
           AVG(avg_price) AS avg_price,
           AVG(avg_price_psf) AS avg_price_psf
    FROM mv_building_12m_summary
    WHERE deal_type='sale' AND rooms='all'
      AND lower(building_name_display) ILIKE %s
    GROUP BY building_name_display, area_name
    ORDER BY deals DESC NULLS LAST
    LIMIT %s
"""

# Trigram: используем `%` оператор + similarity()
_SQL_TRGM = """
    SELECT building_name_display, area_name,
           SUM(deals) AS deals,
           AVG(avg_price) AS avg_price,
           AVG(avg_price_psf) AS avg_price_psf,
           MAX(similarity(lower(building_name_display), %s::text)) AS sim
    FROM mv_building_12m_summary
    WHERE deal_type='sale' AND rooms='all'
      AND lower(building_name_display) %% %s::text
    GROUP BY building_name_display, area_name
    ORDER BY sim DESC NULLS LAST, SUM(deals) DESC NULLS LAST
    LIMIT %s
"""

_SQL_POPULAR = """
    SELECT building_name_display, area_name,
           SUM(deals) AS deals,
           AVG(avg_price) AS avg_price,
           AVG(avg_price_psf) AS avg_price_psf
    FROM mv_building_12m_summary
    WHERE deal_type='sale' AND rooms='all'
    GROUP BY building_name_display, area_name
    ORDER BY deals DESC NULLS LAST
    LIMIT %s
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_building_canonical(
    query: str,
    limit: int = 10,
    area_name: Optional[str] = None,
    include_popular_fallback: bool = True,
) -> List[Dict[str, Any]]:
    """5-уровневый каскадный поиск здания.

    Никогда не возвращает пустой список (если include_popular_fallback=True):
    в худшем случае — top-N популярных зданий с kind="popular".

    Args:
        query: пользовательский ввод (любой регистр).
        limit: макс. число результатов.
        area_name: опц. фильтр по DLD area (например после выбора района).
        include_popular_fallback: если False — возвращаем [] при полном промахе.
    """
    q_raw = (query or "").strip()
    if not q_raw:
        return _popular(limit) if include_popular_fallback else []

    started = time.time()
    out: List[Dict[str, Any]] = []
    seen: set = set()

    def _push(rows: List[Dict[str, Any]], kind: str):
        for r in rows:
            key = ((r.get("building_name_display") or "").lower(),
                   (r.get("area_name") or "").lower())
            if not key[0] or key in seen:
                continue
            if area_name and (r.get("area_name") or "").lower() != area_name.lower():
                continue
            seen.add(key)
            out.append(_wrap_row(r, kind))

    queries = _expand_aliases(q_raw)

    try:
        with _conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # ---------- 1. EXACT (для каждого варианта алиаса) ----------
            for q in queries:
                qn = _clean(q)
                if not qn:
                    continue
                cur.execute(_SQL_EXACT, (qn, limit))
                _push(cur.fetchall(), "exact")
                if len(out) >= limit:
                    break

            # ---------- 2. PREFIX ----------
            if len(out) < limit:
                for q in queries:
                    qn = _clean(q)
                    if not qn:
                        continue
                    cur.execute(_SQL_PREFIX, (qn + "%", limit))
                    _push(cur.fetchall(), "prefix")
                    if len(out) >= limit:
                        break

            # ---------- 3. SUBSTRING ----------
            if len(out) < limit:
                for q in queries:
                    qn = _clean(q)
                    if not qn:
                        continue
                    cur.execute(_SQL_SUBSTR, ("%" + qn + "%", limit))
                    _push(cur.fetchall(), "substring")
                    if len(out) >= limit:
                        break

            # ---------- 4. TRIGRAM fuzzy ----------
            if len(out) < limit:
                for q in queries:
                    qn = _clean(q)
                    if not qn or len(qn) < 3:
                        continue
                    try:
                        cur.execute(_SQL_TRGM, (qn, qn, limit))
                        _push(cur.fetchall(), "fuzzy")
                    except Exception as e:
                        LOG.warning("trgm err for %r: %r", qn, e)
                    if len(out) >= limit:
                        break

            # ---------- 5. POPULAR fallback ----------
            if not out and include_popular_fallback:
                cur.execute(_SQL_POPULAR, (limit,))
                _push(cur.fetchall(), "popular")

            # ---------- master-zone enrichment ----------
            if out:
                mz_map = _master_zone_index(cur, [r["area_name"] for r in out])
                for r in out:
                    a = (r.get("area_name") or "").lower()
                    if a in mz_map:
                        r["master_zone"] = mz_map[a]
    except Exception as e:
        LOG.warning("search_building_canonical err: %r", e)
        return out
    finally:
        dt = (time.time() - started) * 1000
        if dt > 500:
            LOG.warning("[LAT_SLOW] search_building_canonical %.0fms q=%r kinds=%s",
                        dt, query, [r["kind"] for r in out])

    return out[:limit]


def _popular(limit: int) -> List[Dict[str, Any]]:
    """Top-N популярных зданий — используется как полный fallback."""
    out: List[Dict[str, Any]] = []
    try:
        with _conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_SQL_POPULAR, (limit,))
            rows = cur.fetchall()
            for r in rows:
                out.append(_wrap_row(r, "popular"))
            if out:
                mz_map = _master_zone_index(cur, [r["area_name"] for r in out])
                for r in out:
                    a = (r.get("area_name") or "").lower()
                    if a in mz_map:
                        r["master_zone"] = mz_map[a]
    except Exception as e:
        LOG.warning("_popular err: %r", e)
    return out


def format_building_label(item: Dict[str, Any], with_master: bool = True) -> str:
    """Строка для UI: "Burj Khalifa · Downtown Dubai · 21 сделок".

    Удобно для inline-кнопок и подсказок.
    """
    parts = [item["name"]]
    if with_master and item.get("master_zone"):
        parts.append(item["master_zone"])
    elif item.get("area_name"):
        parts.append(item["area_name"])
    if item.get("deals_12m"):
        parts.append(f"{item['deals_12m']} сделок")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    tests = [
        "Burj K",
        "khalifa",
        "Marina Pinacle",  # typo
        "Address opera",
        "Sobha Hartland",
        "xyzabc",          # абракадабра → popular
        "23 Marina",
        "JBR",
        "Atlantis",
        "Бурдж Халифа",
    ]
    for q in tests:
        t = time.time()
        res = search_building_canonical(q, limit=5)
        dt = (time.time() - t) * 1000
        print(f"\n{q!r}  ({dt:.0f}ms)  →  {len(res)} matches")
        for r in res:
            print("   ", format_building_label(r), f"[{r['kind']}]",
                  f"sim={r['similarity']:.2f}" if r.get("similarity") else "")
