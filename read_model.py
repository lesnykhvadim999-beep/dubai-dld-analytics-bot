# -*- coding: utf-8 -*-
"""
read_model.py
=============
Тонкий слой доступа к pre-агрегированной БД (area_stats / building_stats /
market_overview), которую заполняет сервис ``dxb-stats-builder``.

Идея: 95% запросов analytics-бота (обзор района / здания / города,
сравнение, обзорная панель) можно ответить за <50ms из готовых агрегатов
вместо JOIN миллионных raw-таблиц (5-30s).

Использование внутри ``main.py``::

    from read_model import try_area_stats, try_building_stats, READ_MODEL_AVAILABLE

    if READ_MODEL_AVAILABLE:
        row = try_area_stats(area_name, period_months=12, deal_type='sale')
        if row:
            return row
    # fallback: старый путь через raw

Все вызовы безопасны: ловят ошибки и возвращают ``None``/``[]``, чтобы
не сломать бота если read-model недоступна.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras


LOG = logging.getLogger("read_model")


# ------------------------------------------------------------------
# Connection (1 conn / thread, lazy)
# ------------------------------------------------------------------

_LOCAL = threading.local()

READ_MODEL_URL = (
    os.environ.get("READ_MODEL_DATABASE_URL")
    or os.environ.get("ANALYTICS_DATABASE_URL")
    or os.environ.get("ARCHIVE_DATABASE_URL")
)

READ_MODEL_AVAILABLE = bool(READ_MODEL_URL)

# Сколько секунд кешировать «нет в read-model» чтобы не тыкать БД на каждый запрос.
_NEGATIVE_CACHE_TTL = 60
_neg_cache: Dict[str, float] = {}
_neg_lock = threading.Lock()


def _conn():
    # v111: убрали "SELECT 1" перед каждым обращением (это лишний RTT 200-300мс
    # на public proxy). Ping раз в 60с.
    c = getattr(_LOCAL, "conn", None)
    last_ping = getattr(_LOCAL, "conn_ping", 0)
    now_t = time.time()
    if c is not None:
        if (now_t - last_ping) < 60:
            return c
        try:
            with c.cursor() as cur:
                cur.execute("SELECT 1")
            _LOCAL.conn_ping = now_t
            return c
        except Exception:
            try: c.close()
            except Exception: pass
            _LOCAL.conn = None
    if not READ_MODEL_URL:
        raise RuntimeError("READ_MODEL_DATABASE_URL is not configured")
    c = psycopg2.connect(
        READ_MODEL_URL,
        connect_timeout=5,
        application_name="dld-analytics-bot[read-model]",
    )
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("SET statement_timeout = 5000")  # 5 сек — read-model должно отдавать мгновенно
    _LOCAL.conn = c
    _LOCAL.conn_ping = now_t
    return c


def _neg_hit(key: str) -> bool:
    with _neg_lock:
        exp = _neg_cache.get(key)
        if exp and exp > time.time():
            return True
        if exp:
            _neg_cache.pop(key, None)
    return False


def _neg_set(key: str) -> None:
    with _neg_lock:
        _neg_cache[key] = time.time() + _NEGATIVE_CACHE_TTL


# ------------------------------------------------------------------
# Нормализаторы
# ------------------------------------------------------------------

def _area_key(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    return name.strip().lower()


def _norm_rooms(rooms: Optional[str]) -> str:
    if not rooms:
        return "all"
    r = rooms.strip().lower()
    if "studio" in r:
        return "studio"
    if r.startswith("1") or "1br" in r or "1 b" in r or "1 bed" in r:
        return "1br"
    if r.startswith("2") or "2br" in r or "2 b" in r or "2 bed" in r:
        return "2br"
    if r.startswith("3") or "3br" in r or "3 b" in r or "3 bed" in r:
        return "3br"
    if r.startswith("4") or "4br" in r or "4 b" in r or "4 bed" in r:
        return "4br"
    if r.startswith(("5", "6", "7")):
        return "4br+"
    return "all"


def _norm_prop(prop: Optional[str]) -> str:
    if not prop:
        return "all"
    p = prop.strip().lower()
    if "villa" in p:
        return "villa"
    if "town" in p:
        return "townhouse"
    if "office" in p:
        return "office"
    if "shop" in p or "retail" in p:
        return "retail"
    if "apart" in p or "flat" in p or "unit" in p or p == "residential":
        return "apartment"
    return "all"


def _norm_deal(deal_type: Optional[str]) -> str:
    if not deal_type:
        return "sale"
    d = deal_type.strip().lower()
    if d.startswith("rent") or d.startswith("ejari") or d.startswith("lease"):
        return "rent"
    return "sale"


def _period_start(months_back: int) -> dt.date:
    today = dt.date.today().replace(day=1)
    y, m = today.year, today.month
    for _ in range(months_back - 1):
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return dt.date(y, m, 1)


# ------------------------------------------------------------------
# Высокоуровневые API
# ------------------------------------------------------------------

def try_area_stats(area_name: str,
                   period_months: int = 12,
                   prop: Optional[str] = None,
                   rooms: Optional[str] = None,
                   deal_type: Optional[str] = None,
                   ) -> Optional[Dict[str, Any]]:
    """Aggregate area_stats за последние N месяцев.

    Возвращает dict с тем же набором полей, что и raw путь:
        deals, avg_price, median_price, top_quartile_price,
        avg_price_psf, top_quartile_psf, yoy_growth_pct,
        yoy_growth_top_pct, avg_rental_yield_pct, top_rental_yield_pct,
        area_name (display), period_months, source='read_model'

    None если данных нет или read-model недоступна.
    """
    if not READ_MODEL_AVAILABLE:
        return None
    ak = _area_key(area_name)
    if not ak:
        return None
    cache_key = f"area:{ak}:{period_months}:{prop}:{rooms}:{deal_type}"
    if _neg_hit(cache_key):
        return None

    start = _period_start(period_months)
    pt = _norm_prop(prop)
    rm = _norm_rooms(rooms)
    dl = _norm_deal(deal_type)

    # P1: single source of truth — same MV as resale-bot reads. Eliminates
    # AVG(median_price) bug (медиана медиан != медиана) and ensures cross-bot
    # consistency. mv_area_12m_summary is pre-aggregated per
    # (area_key, rooms, property_type, deal_type), so no aggregation needed.
    # Falls back to legacy area_stats SUM/AVG if MV row missing.
    mv_sql = """
        SELECT
            area_name,
            deals                                AS deals,
            avg_price                            AS avg_price,
            avg_price                            AS median_price,
            top_quartile_price                   AS top_quartile_price,
            avg_price_psf                        AS avg_price_psf,
            top_quartile_psf                     AS top_quartile_psf,
            yoy_growth_pct                       AS yoy_growth_pct,
            yoy_growth_top_pct                   AS yoy_growth_top_pct,
            avg_rental_yield_pct                 AS avg_rental_yield_pct,
            top_rental_yield_pct                 AS top_rental_yield_pct,
            NULL::timestamptz                    AS computed_at
        FROM mv_area_12m_summary
        WHERE area_key = %s
          AND property_type = %s
          AND rooms = %s
          AND deal_type = %s
    """
    legacy_sql = """
        SELECT
            MIN(area_name)                                                       AS area_name,
            SUM(deals_count)::int                                                AS deals,
            (SUM(avg_price_aed * deals_count) / NULLIF(SUM(deals_count),0))      AS avg_price,
            (SUM(median_price_aed * deals_count) / NULLIF(SUM(deals_count),0))   AS median_price,
            MAX(top_quartile_price_aed)                                          AS top_quartile_price,
            (SUM(avg_price_psf * deals_count) / NULLIF(SUM(deals_count),0))      AS avg_price_psf,
            MAX(top_quartile_psf)                                                AS top_quartile_psf,
            AVG(yoy_growth_pct)                                                  AS yoy_growth_pct,
            MAX(yoy_growth_top_pct)                                              AS yoy_growth_top_pct,
            AVG(avg_rental_yield_pct)                                            AS avg_rental_yield_pct,
            MAX(top_rental_yield_pct)                                            AS top_rental_yield_pct,
            MAX(computed_at)                                                     AS computed_at
        FROM area_stats
        WHERE area_key = %s
          AND property_type = %s
          AND rooms = %s
          AND deal_type = %s
          AND period_month >= %s
    """
    row = None
    try:
        with _conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Try MV first (only valid for 12m, default period)
            if period_months == 12:
                try:
                    cur.execute(mv_sql, (ak, pt, rm, dl))
                    row = cur.fetchone()
                except Exception as mv_e:
                    LOG.debug("mv_area_12m_summary query failed, fallback to area_stats: %s", mv_e)
                    row = None
            if not row or not row.get("deals"):
                cur.execute(legacy_sql, (ak, pt, rm, dl, start))
                row = cur.fetchone()
    except Exception as e:
        LOG.warning("try_area_stats failed: %s", e)
        return None

    if not row or not row.get("deals"):
        _neg_set(cache_key)
        return None

    row = dict(row)
    row["period_months"] = period_months
    row["source"] = "read_model"
    return row


def try_building_stats(building_name: str,
                       period_months: int = 12,
                       rooms: Optional[str] = None,
                       deal_type: Optional[str] = None,
                       ) -> Optional[Dict[str, Any]]:
    """Aggregate building_stats за последние N месяцев."""
    if not READ_MODEL_AVAILABLE:
        return None
    bk = _area_key(building_name)  # тот же normalization
    if not bk:
        return None
    cache_key = f"building:{bk}:{period_months}:{rooms}:{deal_type}"
    if _neg_hit(cache_key):
        return None

    start = _period_start(period_months)
    rm = _norm_rooms(rooms)
    dl = _norm_deal(deal_type)

    sql = """
        SELECT
            MIN(building_name_display)                                          AS building_name,
            MIN(area_name)                                                      AS area_name,
            SUM(deals_count)::int                                               AS deals,
            (SUM(avg_price_aed*deals_count)/NULLIF(SUM(deals_count),0))         AS avg_price,
            AVG(median_price_aed)                                               AS median_price,
            MAX(top_quartile_price_aed)                                         AS top_quartile_price,
            AVG(avg_price_psf)                                                  AS avg_price_psf,
            MAX(top_quartile_psf)                                               AS top_quartile_psf,
            MAX(last_deal_date)                                                 AS last_deal_date,
            MAX(computed_at)                                                    AS computed_at
        FROM building_stats
        WHERE building_key = %s
          AND rooms = %s
          AND deal_type = %s
          AND period_month >= %s
    """
    try:
        with _conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (bk, rm, dl, start))
            row = cur.fetchone()
    except Exception as e:
        LOG.warning("try_building_stats failed: %s", e)
        return None

    if not row or not row.get("deals"):
        _neg_set(cache_key)
        return None

    row = dict(row)
    row["period_months"] = period_months
    row["source"] = "read_model"
    return row


def try_market_overview(period_months: int = 12) -> Optional[Dict[str, Any]]:
    """Market overview Дубая (агрегат всех area_stats)."""
    if not READ_MODEL_AVAILABLE:
        return None
    cache_key = f"market:{period_months}"
    if _neg_hit(cache_key):
        return None
    start = _period_start(period_months)
    sql = """
        SELECT
            SUM(total_deals)::int                                              AS deals,
            SUM(total_volume_aed)                                              AS total_volume,
            (SUM(total_volume_aed)/NULLIF(SUM(total_deals),0))                 AS avg_price,
            AVG(median_price_aed)                                              AS median_price,
            MAX(top_quartile_price_aed)                                        AS top_quartile_price,
            AVG(yoy_growth_pct)                                                AS yoy_growth_pct,
            MAX(top_growth_pct)                                                AS yoy_growth_top_pct,
            MAX(computed_at)                                                   AS computed_at
        FROM market_overview
        WHERE period_month >= %s
    """
    try:
        with _conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (start,))
            row = cur.fetchone()
    except Exception as e:
        LOG.warning("try_market_overview failed: %s", e)
        return None
    if not row or not row.get("deals"):
        _neg_set(cache_key)
        return None
    row = dict(row)
    row["period_months"] = period_months
    row["source"] = "read_model"
    return row


def top_buildings_in_area(area_name: str,
                          period_months: int = 6,
                          deal_type: Optional[str] = None,
                          limit: int = 10) -> List[Dict[str, Any]]:
    """Топ-N зданий в районе по числу сделок (для UI 'самые активные')."""
    if not READ_MODEL_AVAILABLE:
        return []
    ak = _area_key(area_name)
    if not ak:
        return []
    dl = _norm_deal(deal_type)
    start = _period_start(period_months)
    sql = """
        SELECT
            building_key,
            MIN(building_name_display) AS building_name,
            SUM(deals_count)::int      AS deals,
            (SUM(avg_price_aed*deals_count)/NULLIF(SUM(deals_count),0)) AS avg_price,
            MAX(top_quartile_price_aed) AS top_quartile_price,
            MAX(last_deal_date)         AS last_deal_date
        FROM building_stats
        WHERE area_key = %s
          AND deal_type = %s
          AND rooms = 'all'
          AND period_month >= %s
        GROUP BY building_key
        HAVING SUM(deals_count) > 0
        ORDER BY deals DESC
        LIMIT %s
    """
    try:
        with _conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (ak, dl, start, limit))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        LOG.warning("top_buildings_in_area failed: %s", e)
        return []


def is_available() -> bool:
    """Health-check для бота на старте."""
    if not READ_MODEL_AVAILABLE:
        return False
    try:
        with _conn().cursor() as cur:
            cur.execute("SELECT count(*) FROM area_stats")
            n = cur.fetchone()[0]
            return n > 0
    except Exception as e:
        LOG.warning("read_model not available: %s", e)
        return False
