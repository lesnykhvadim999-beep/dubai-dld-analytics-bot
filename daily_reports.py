"""Daily market reports generator + storage (v2 — schema-aware).

Генерирует полноценные ежедневные снимки рынка и хранит их в intelligence DB.
Использует динамическое обнаружение колонок — работает на любой версии DLD-схемы.

Скоупы:
  - 'dubai'    — весь Дубай
  - 'area'     — конкретный район
  - 'building' — конкретное здание

Хранилище: таблица daily_market_reports в intelligence DB.

Архив только читаем — не модифицируем (constraint).
"""
from __future__ import annotations
import os
import sys
import logging
import re
from datetime import datetime, timedelta, date
from typing import Optional, List, Dict, Any, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor, Json

# Use Asia/Dubai for market-day boundaries (DLD instance_date is in Asia/Dubai).
# Without this, daily reports built around midnight UTC drop transactions from
# 20:00–23:59 Asia/Dubai of the last calendar day.
try:
    from tz_utils import now_dubai, today_dubai  # type: ignore
except ImportError:
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
        from tz_utils import now_dubai, today_dubai  # type: ignore
    except ImportError:
        from zoneinfo import ZoneInfo
        _DUBAI = ZoneInfo("Asia/Dubai")
        def now_dubai():  # type: ignore
            return datetime.now(_DUBAI)
        def today_dubai():  # type: ignore
            return now_dubai().date()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("daily_reports")


# ── DB connections ────────────────────────────────────────────────────────
def _live_conn():
    return psycopg2.connect(
        os.environ.get("LIVE_DATABASE_URL") or os.environ.get("DATABASE_URL"),
        cursor_factory=RealDictCursor,
    )


def _archive_conn():
    return psycopg2.connect(
        os.environ.get("ARCHIVE_DATABASE_URL") or os.environ.get("DATABASE_URL"),
        cursor_factory=RealDictCursor,
    )


def _intel_conn():
    return psycopg2.connect(
        os.environ.get("INTELLIGENCE_DATABASE_URL") or os.environ.get("DATABASE_URL"),
        cursor_factory=RealDictCursor,
    )


# ── Schema setup ──────────────────────────────────────────────────────────
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS daily_market_reports (
    id              BIGSERIAL PRIMARY KEY,
    scope           TEXT NOT NULL,
    entity_name     TEXT,
    entity_key      TEXT,
    report_date     DATE NOT NULL,
    report          JSONB NOT NULL,
    deal_count_30d  INTEGER,
    median_price    NUMERIC,
    median_psf      NUMERIC,
    updated_at      TIMESTAMP DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_dmr_scope_key_date
    ON daily_market_reports(scope, entity_key, report_date);
CREATE INDEX IF NOT EXISTS idx_dmr_date
    ON daily_market_reports(report_date DESC);
CREATE INDEX IF NOT EXISTS idx_dmr_lookup
    ON daily_market_reports(scope, entity_key);
"""


def ensure_schema():
    with _intel_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_SQL)
        conn.commit()
    log.info("daily_market_reports schema ensured")


def _entity_key(scope: str, name: Optional[str]) -> str:
    if scope == "dubai":
        return "dubai"
    return f"{scope}:{(name or '').strip().lower()}"


# ── Column candidates (same approach as main.py v44 schema-aware layer) ─
COL_CANDIDATES = {
    "date":     ["transaction_date", "instance_date", "registration_date", "date", "created_at",
                 "contract_start_date", "contract_end_date"],
    "area":     ["area_name_en", "area_en", "area_name", "area", "master_project_en", "master_project"],
    "building": ["building_name_en", "building_en", "building_name", "building",
                 "project_name_en", "project_en", "project_name", "project"],
    "rooms":    ["rooms_en", "rooms", "bedrooms", "bedroom", "beds"],
    "price":    ["actual_worth", "amount", "price", "sale_price", "value",
                 "transaction_amount", "procedure_value", "trans_value"],
    "psf":      ["meter_sale_price"],  # AED per sqm-meter actually in DLD, name kept for compat
    "rent":     ["annual_amount", "contract_amount", "rent_value", "rent_amount",
                 "amount", "annual_rent", "contract_value"],
    "size_sqm": ["actual_area", "procedure_area", "area_sqm", "property_size_sqm"],
}


def _table_cols(conn, table_name: str) -> List[str]:
    """Returns list of column names for given table (lowercase)."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name=%s AND table_schema='public'
                ORDER BY ordinal_position
            """, (table_name,))
            return [r["column_name"] for r in cur.fetchall()]
    except Exception as e:
        log.warning(f"table_cols({table_name}): {e}")
        return []


def _first_col(cols: List[str], candidates: List[str]) -> Optional[str]:
    low = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand.lower() in low:
            return low[cand.lower()]
    return None


def _date_expr(col: str) -> str:
    """Robust TEXT-or-DATE date column → date. Works with '2024-01-15' and '01/15/2024' formats."""
    q = f'"{col}"'
    return f"""
        CASE
            WHEN {q}::text ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' THEN {q}::text::date
            WHEN {q}::text ~ '^\\d{{2}}/\\d{{2}}/\\d{{4}}' THEN TO_DATE(SUBSTRING({q}::text, 1, 10), 'MM/DD/YYYY')
            ELSE NULL::date
        END
    """


def _num_expr(col: str) -> str:
    q = f'"{col}"'
    return f"NULLIF(regexp_replace(COALESCE({q}::text, ''), '[^0-9.]', '', 'g'), '')::numeric"


# ── Period helpers ────────────────────────────────────────────────────────
def _periods(today: Optional[date] = None) -> dict:
    today = today or today_dubai()  # Dubai market-day boundary, not UTC
    m_start = today.replace(day=1)
    if m_start.month == 1:
        m_prev_start = m_start.replace(year=m_start.year - 1, month=12)
    else:
        m_prev_start = m_start.replace(month=m_start.month - 1)
    y_prev_start = m_start.replace(year=m_start.year - 1)
    if y_prev_start.month == 12:
        y_prev_end = y_prev_start.replace(year=y_prev_start.year + 1, month=1)
    else:
        y_prev_end = y_prev_start.replace(month=y_prev_start.month + 1)
    return {
        "today": today,
        "month_start": m_start,
        "month_prev_start": m_prev_start, "month_prev_end": m_start,
        "year_prev_start": y_prev_start, "year_prev_end": y_prev_end,
        "d30":  today - timedelta(days=30),
        "d90":  today - timedelta(days=90),
        "d365": today - timedelta(days=365),
    }


# ── Bedroom normalization ─────────────────────────────────────────────────
def _normalize_rooms(raw: Optional[str]) -> str:
    if not raw:
        return "Unknown"
    s = str(raw).lower().strip()
    if "studio" in s:
        return "Studio"
    m = re.search(r"(\d+)", s)
    if not m:
        return "Unknown"
    n = int(m.group(1))
    if n <= 0:
        return "Studio"
    if n >= 4:
        return "4BR+"
    return f"{n}BR"


BEDROOM_BUCKETS = ["Studio", "1BR", "2BR", "3BR", "4BR+"]


# ── Schema-aware aggregation ──────────────────────────────────────────────
class TableSchema:
    """Schema info for one table, computed once per connection."""
    def __init__(self, conn, table: str):
        self.table = table
        cols = _table_cols(conn, table)
        self.cols = cols
        self.date_col      = _first_col(cols, COL_CANDIDATES["date"])
        self.area_col      = _first_col(cols, COL_CANDIDATES["area"])
        self.building_col  = _first_col(cols, COL_CANDIDATES["building"])
        self.rooms_col     = _first_col(cols, COL_CANDIDATES["rooms"])
        self.price_col     = _first_col(cols, COL_CANDIDATES["price"])
        self.psf_col       = _first_col(cols, COL_CANDIDATES["psf"])
        self.rent_col      = _first_col(cols, COL_CANDIDATES["rent"])

    def ok_sales(self) -> bool:
        return all([self.date_col, self.price_col])

    def ok_rents(self) -> bool:
        return all([self.date_col, self.rent_col])

    def __repr__(self):
        return (f"TableSchema({self.table}: date={self.date_col} price={self.price_col} "
                f"area={self.area_col} bldg={self.building_col} rooms={self.rooms_col})")


def _aggregate_sales(conn, sch: TableSchema, start: date, end: date,
                     scope: str, name: Optional[str]) -> Dict[str, dict]:
    if not sch.ok_sales():
        return {}
    rooms_expr = f'"{sch.rooms_col}"' if sch.rooms_col else "''::text"
    price_expr = _num_expr(sch.price_col)
    psf_expr   = _num_expr(sch.psf_col) if sch.psf_col else "NULL::numeric"
    date_expr  = _date_expr(sch.date_col)

    scope_clause, args = "", ()
    if scope == "area" and sch.area_col and name:
        scope_clause = f' AND LOWER(TRIM("{sch.area_col}"::text)) = LOWER(TRIM(%s))'
        args = (name,)
    elif scope == "building" and sch.building_col and name:
        scope_clause = f' AND LOWER(TRIM("{sch.building_col}"::text)) = LOWER(TRIM(%s))'
        args = (name,)
    elif scope in ("area", "building") and (name and not (sch.area_col or sch.building_col)):
        return {}

    sql = f"""
        SELECT
            {rooms_expr}                                               AS rooms_raw,
            COUNT(*)                                                   AS n,
            AVG({price_expr})                                          AS avg_price,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {price_expr})  AS median_price,
            AVG({psf_expr})                                            AS avg_psf,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {psf_expr})    AS median_psf,
            MIN({price_expr})                                          AS min_price,
            MAX({price_expr})                                          AS max_price
        FROM {sch.table}
        WHERE {date_expr} >= %s AND {date_expr} < %s
          AND {price_expr} > 100000
          {scope_clause}
        GROUP BY {rooms_expr}
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (start, end) + args)
            rows = cur.fetchall()
    except Exception as e:
        conn.rollback()  # critical: clear failed-transaction state
        log.warning(f"aggregate_sales {sch.table} err: {e}")
        return {}

    buckets: Dict[str, dict] = {}
    for r in rows:
        bucket = _normalize_rooms(r.get("rooms_raw"))
        b = buckets.setdefault(bucket, {"n": 0, "avg_price": 0.0, "median_price": 0.0,
                                         "avg_psf": 0.0, "median_psf": 0.0,
                                         "min_price": None, "max_price": None,
                                         "_acc_avg_price": 0.0, "_acc_avg_psf": 0.0,
                                         "_acc_med_price": 0.0, "_acc_med_psf": 0.0})
        n_r = int(r.get("n") or 0)
        if r.get("avg_price"):
            b["_acc_avg_price"] += float(r["avg_price"]) * n_r
        if r.get("avg_psf"):
            b["_acc_avg_psf"] += float(r["avg_psf"]) * n_r
        # v52 FIX: weighted-by-count вместо MAX (раньше inflated price на 3-7%)
        if r.get("median_price"):
            b["_acc_med_price"] += float(r["median_price"]) * n_r
        if r.get("median_psf"):
            b["_acc_med_psf"] += float(r["median_psf"]) * n_r
        if r.get("min_price"):
            mp = float(r["min_price"])
            b["min_price"] = mp if b["min_price"] is None else min(b["min_price"], mp)
        if r.get("max_price"):
            mp = float(r["max_price"])
            b["max_price"] = mp if b["max_price"] is None else max(b["max_price"], mp)
        b["n"] += n_r
    for b in buckets.values():
        if b["n"] > 0:
            b["avg_price"]    = b["_acc_avg_price"] / b["n"] if b["_acc_avg_price"] else None
            b["avg_psf"]      = b["_acc_avg_psf"]   / b["n"] if b["_acc_avg_psf"] else None
            b["median_price"] = b["_acc_med_price"] / b["n"] if b["_acc_med_price"] else None
            b["median_psf"]   = b["_acc_med_psf"]   / b["n"] if b["_acc_med_psf"] else None
        for k in ("_acc_avg_price", "_acc_avg_psf", "_acc_med_price", "_acc_med_psf"):
            b.pop(k, None)
    return buckets


def _aggregate_rents(conn, sch: TableSchema, start: date, end: date,
                     scope: str, name: Optional[str]) -> Dict[str, dict]:
    if not sch.ok_rents():
        return {}
    rooms_expr = f'"{sch.rooms_col}"' if sch.rooms_col else "''::text"
    rent_expr  = _num_expr(sch.rent_col)
    date_expr  = _date_expr(sch.date_col)

    scope_clause, args = "", ()
    if scope == "area" and sch.area_col and name:
        scope_clause = f' AND LOWER(TRIM("{sch.area_col}"::text)) = LOWER(TRIM(%s))'
        args = (name,)
    elif scope == "building" and sch.building_col and name:
        scope_clause = f' AND LOWER(TRIM("{sch.building_col}"::text)) = LOWER(TRIM(%s))'
        args = (name,)
    elif scope in ("area", "building") and (name and not (sch.area_col or sch.building_col)):
        return {}

    sql = f"""
        SELECT
            {rooms_expr}                                              AS rooms_raw,
            COUNT(*)                                                  AS n,
            AVG({rent_expr})                                          AS avg_rent,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {rent_expr})  AS median_rent
        FROM {sch.table}
        WHERE {date_expr} >= %s AND {date_expr} < %s
          AND {rent_expr} > 10000
          {scope_clause}
        GROUP BY {rooms_expr}
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (start, end) + args)
            rows = cur.fetchall()
    except Exception as e:
        conn.rollback()
        log.warning(f"aggregate_rents {sch.table} err: {e}")
        return {}

    buckets: Dict[str, dict] = {}
    for r in rows:
        bucket = _normalize_rooms(r.get("rooms_raw"))
        b = buckets.setdefault(bucket, {"n": 0, "avg_rent": 0.0, "median_rent": 0.0,
                                         "_acc_avg": 0.0})
        n_r = int(r.get("n") or 0)
        if r.get("avg_rent"):
            b["_acc_avg"] += float(r["avg_rent"]) * n_r
        if r.get("median_rent"):
            b["median_rent"] = max(b.get("median_rent") or 0, float(r["median_rent"]))
        b["n"] += n_r
    for b in buckets.values():
        if b["n"] > 0:
            b["avg_rent"] = b["_acc_avg"] / b["n"] if b["_acc_avg"] else None
        b.pop("_acc_avg", None)
    return buckets


def _merge_buckets(a: Dict[str, dict], b: Dict[str, dict],
                   fields_avg=("avg_price", "avg_psf", "avg_rent"),
                   fields_max=("median_price", "median_psf", "median_rent", "max_price"),
                   fields_min=("min_price",)) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for k in set(list(a.keys()) + list(b.keys())):
        ba, bb = a.get(k, {}), b.get(k, {})
        n_a, n_b = ba.get("n", 0) or 0, bb.get("n", 0) or 0
        n_t = n_a + n_b
        rec = {"n": n_t}
        for f in fields_avg:
            va, vb = ba.get(f), bb.get(f)
            if va is None and vb is None: rec[f] = None
            elif va is None: rec[f] = vb
            elif vb is None: rec[f] = va
            elif n_t == 0: rec[f] = None
            else: rec[f] = (va * n_a + vb * n_b) / n_t
        for f in fields_max:
            xs = [x for x in [ba.get(f), bb.get(f)] if x is not None]
            rec[f] = max(xs) if xs else None
        for f in fields_min:
            xs = [x for x in [ba.get(f), bb.get(f)] if x is not None]
            rec[f] = min(xs) if xs else None
        out[k] = rec
    return out


def _pct_change(curr, prev):
    if not prev or prev == 0: return None
    if curr is None: curr = 0
    return round((float(curr) - float(prev)) / float(prev) * 100, 1)


# ── Build report for one entity ───────────────────────────────────────────
def build_report(scope: str, name: Optional[str] = None) -> Tuple[dict, int, Optional[float], Optional[float]]:
    p = _periods()

    # Discover schemas (one query per DB per process, but called per entity here for simplicity)
    schemas = {}
    for kind, fn, tbls in [
        ("sale",  _archive_conn, ["dld_sale_archive"]),
        ("sale",  _live_conn,    ["dld_transactions_full"]),
        ("rent",  _archive_conn, ["dld_rent_archive"]),
        ("rent",  _live_conn,    ["dld_rents_full"]),
    ]:
        for t in tbls:
            try:
                with fn() as c:
                    schemas[(kind, t)] = (fn, TableSchema(c, t))
            except Exception as e:
                log.debug(f"schema {t} skip: {e}")

    # Sales: 30d / 90d / 365d / month_prev / year_prev (same calendar month)
    def agg_combined(kind: str, start, end):
        out = {}
        for (k, tbl), (fn, sch) in schemas.items():
            if k != kind: continue
            try:
                with fn() as c:
                    b = (_aggregate_sales if kind == "sale" else _aggregate_rents)(c, sch, start, end, scope, name)
                    out = _merge_buckets(out, b) if out else b
            except Exception as e:
                log.debug(f"agg_combined {kind} {tbl}: {e}")
        return out

    sales_30d  = agg_combined("sale", p["d30"],  p["today"])
    sales_90d  = agg_combined("sale", p["d90"],  p["today"])
    sales_365d = agg_combined("sale", p["d365"], p["today"])
    sales_month_prev = agg_combined("sale", p["month_prev_start"], p["month_prev_end"])
    sales_year_prev  = agg_combined("sale", p["year_prev_start"],  p["year_prev_end"])

    rents_365d = agg_combined("rent", p["d365"], p["today"])

    # ROI per bedroom
    roi: Dict[str, Optional[float]] = {}
    for bucket in BEDROOM_BUCKETS:
        rent = (rents_365d.get(bucket) or {}).get("median_rent") or (rents_365d.get(bucket) or {}).get("avg_rent")
        sale = (sales_365d.get(bucket) or {}).get("median_price") or (sales_365d.get(bucket) or {}).get("avg_price")
        roi[bucket] = round(float(rent) / float(sale) * 100, 2) if (rent and sale and sale > 0) else None

    # totals — v52 FIX: weighted by count, не simple average
    # Раньше (1BR 100шт @ 1M) + (4BR 5шт @ 3M) → (1M+3M)/2 = 2M (wrong)
    # Правильно: (1M*100 + 3M*5)/105 = 1.095M
    def totals(d):
        n = sum((d.get(b) or {}).get("n", 0) for b in BEDROOM_BUCKETS)
        # weighted median price
        med_acc = sum(
            (((d.get(b) or {}).get("median_price") or 0) * ((d.get(b) or {}).get("n") or 0))
            for b in BEDROOM_BUCKETS
        )
        med = (med_acc / n) if n else None
        # weighted median psf
        psf_acc = sum(
            (((d.get(b) or {}).get("median_psf") or 0) * ((d.get(b) or {}).get("n") or 0))
            for b in BEDROOM_BUCKETS
        )
        psf = (psf_acc / n) if n else None
        return n, med, psf

    n30, med30, psf30 = totals(sales_30d)
    n_mp, med_mp, _ = totals(sales_month_prev)
    n_yp, med_yp, _ = totals(sales_year_prev)
    n90, _, _ = totals(sales_90d)
    n365, _, _ = totals(sales_365d)

    report = {
        "scope": scope, "entity": name,
        "generated_at": datetime.utcnow().isoformat(),
        "report_date": p["today"].isoformat(),
        "totals": {
            "deals_30d": n30, "deals_90d": n90, "deals_365d": n365,
            "median_price_30d": med30, "median_psf_30d": psf30,
        },
        "bedroom_breakdown": {
            bucket: {
                "sales_30d":  sales_30d.get(bucket)  or {},
                "sales_90d":  sales_90d.get(bucket)  or {},
                "sales_365d": sales_365d.get(bucket) or {},
                "rent_365d":  rents_365d.get(bucket) or {},
                "roi_pct_365d": roi.get(bucket),
            } for bucket in BEDROOM_BUCKETS
        },
        "dynamics": {
            "vs_prev_month": {
                "deals_change_pct":  _pct_change(n30, n_mp),
                "median_change_pct": _pct_change(med30, med_mp),
            },
            "vs_year_ago": {
                "deals_change_pct":  _pct_change(n30, n_yp),
                "median_change_pct": _pct_change(med30, med_yp),
            },
        },
    }
    return report, n30, med30, psf30


# ── Storage ───────────────────────────────────────────────────────────────
def save_report(scope: str, name: Optional[str], report: dict,
                deal_count_30d: Optional[int] = None,
                median_price: Optional[float] = None,
                median_psf: Optional[float] = None):
    today = today_dubai()  # report_date must align with Dubai market day
    key = _entity_key(scope, name)
    with _intel_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO daily_market_reports
                    (scope, entity_name, entity_key, report_date, report,
                     deal_count_30d, median_price, median_psf, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (scope, entity_key, report_date)
                DO UPDATE SET
                    report = EXCLUDED.report,
                    deal_count_30d = EXCLUDED.deal_count_30d,
                    median_price   = EXCLUDED.median_price,
                    median_psf     = EXCLUDED.median_psf,
                    updated_at = now()
            """, (scope, name, key, today, Json(report),
                  deal_count_30d, median_price, median_psf))
        conn.commit()


# ── Discovery helpers (top areas / top buildings) ─────────────────────────
def _list_top(kind: str, limit: int) -> List[str]:
    """kind ∈ {'area', 'building'}. Returns top names by 365d transaction count."""
    p = _periods()
    counts: Dict[str, int] = {}
    for fn, tbl in [(_archive_conn, "dld_sale_archive"),
                    (_live_conn,    "dld_transactions_full")]:
        try:
            with fn() as conn:
                sch = TableSchema(conn, tbl)
                if not sch.ok_sales():
                    continue
                target_col = sch.area_col if kind == "area" else sch.building_col
                if not target_col:
                    continue
                date_expr = _date_expr(sch.date_col)
                with conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT "{target_col}" AS name, COUNT(*) AS n
                          FROM {tbl}
                         WHERE {date_expr} >= %s
                           AND NULLIF("{target_col}"::text,'') IS NOT NULL
                         GROUP BY "{target_col}"
                    """, (p["d365"],))
                    for r in cur.fetchall():
                        nm = (r["name"] or "").strip()
                        if not nm: continue
                        counts[nm] = counts.get(nm, 0) + int(r["n"])
        except Exception as e:
            log.warning(f"list_top {kind} {tbl}: {e}")
    return [n for n, _ in sorted(counts.items(), key=lambda x: -x[1])[:limit]]


def _list_top_areas(limit: int = 200) -> List[str]:
    return _list_top("area", limit)


def _list_top_buildings(limit: int = 500) -> List[str]:
    return _list_top("building", limit)


# ── Daily orchestrator ────────────────────────────────────────────────────
def run_daily(max_areas: int = 150, max_buildings: int = 300):
    ensure_schema()
    started = datetime.utcnow()
    log.info(f"=== daily_reports.run_daily started at {started.isoformat()} ===")
    # Cron heartbeat (best-effort; never blocks reports)
    try:
        from auto_audit._common import record_metric
        record_metric("cron.tick.daily_reports", 1.0, meta={"status": "ok"})
    except Exception:
        pass

    # 1. Dubai-wide
    try:
        report, n, med, psf = build_report("dubai")
        save_report("dubai", None, report, n, med, psf)
        log.info(f"saved dubai report: deals_30d={n} median={med}")
    except Exception as e:
        log.error(f"dubai report err: {e}")

    # 2. Areas
    areas = _list_top_areas(max_areas)
    log.info(f"generating reports for {len(areas)} areas...")
    for i, area in enumerate(areas, 1):
        try:
            report, n, med, psf = build_report("area", area)
            save_report("area", area, report, n, med, psf)
            if i % 25 == 0:
                log.info(f"  [{i}/{len(areas)}] saved area={area} deals30d={n}")
        except Exception as e:
            log.warning(f"  area {area} err: {e}")

    # 3. Buildings
    buildings = _list_top_buildings(max_buildings)
    log.info(f"generating reports for {len(buildings)} buildings...")
    for i, b in enumerate(buildings, 1):
        try:
            report, n, med, psf = build_report("building", b)
            save_report("building", b, report, n, med, psf)
            if i % 50 == 0:
                log.info(f"  [{i}/{len(buildings)}] saved building={b} deals30d={n}")
        except Exception as e:
            log.warning(f"  building {b} err: {e}")

    elapsed = (datetime.utcnow() - started).total_seconds()
    log.info(f"=== daily_reports.run_daily finished in {elapsed:.1f}s ===")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-areas", type=int, default=150)
    ap.add_argument("--limit-buildings", type=int, default=300)
    args = ap.parse_args()
    run_daily(args.limit_areas, args.limit_buildings)
