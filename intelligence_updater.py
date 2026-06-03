import os
import time
import math
import logging
from datetime import datetime, UTC
from typing import Dict, List, Optional, Tuple, Any

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

LIVE_DATABASE_URL = os.getenv("LIVE_DATABASE_URL") or os.getenv("DATABASE_URL")
ARCHIVE_DATABASE_URL = os.getenv("ARCHIVE_DATABASE_URL") or os.getenv("ARCHIVE_DB_URL") or os.getenv("DATABASE_URL")
INTELLIGENCE_DATABASE_URL = os.getenv("INTELLIGENCE_DATABASE_URL") or os.getenv("INTELLIGENCE_DB_URL") or os.getenv("DATABASE_URL")
CYCLE_SECONDS = int(os.getenv("INTELLIGENCE_CYCLE_SECONDS", "21600"))
STATEMENT_TIMEOUT_MS = int(os.getenv("INTELLIGENCE_STATEMENT_TIMEOUT_MS", "0"))

SALE_SOURCES = [("archive", ARCHIVE_DATABASE_URL, "public", "dld_sale_archive"), ("live", LIVE_DATABASE_URL, "public", "dld_transactions_full")]
RENT_SOURCES = [("archive", ARCHIVE_DATABASE_URL, "public", "dld_rent_archive"), ("live", LIVE_DATABASE_URL, "public", "dld_rents_full")]
PERIODS = {"30d": 30, "90d": 90, "180d": 180, "365d": 365}

COLUMN_CANDIDATES = {
    "date": ["transaction_date", "instance_date", "procedure_date", "date", "registration_date", "contract_start_date", "contract_end_date"],
    "area": ["area_name_en", "area_en", "area_name", "area", "master_project_en", "master_project", "community"],
    "building": ["building_name_en", "building_en", "building_name", "building", "project_name_en", "project_en", "project_name", "project", "master_project_en"],
    "project": ["project_name_en", "project_en", "project_name", "project", "master_project_en", "master_project"],
    "property_type": ["property_type_en", "prop_type_en", "property_type", "property_usage_en", "property_usage", "usage_en"],
    "unit_type": ["property_sub_type_en", "prop_sub_type_en", "property_sub_type", "unit_type_en", "unit_type", "property_type_en", "prop_type_en", "property_type"],
    "rooms": ["rooms_en", "rooms", "bedrooms", "bedroom", "beds", "room"],
    "size_sqm": ["actual_area", "procedure_area", "area_sqm", "property_size_sqm", "size_sqm", "property_area", "area"],
    "size_sqft": ["area_sqft", "property_size_sqft", "size_sqft", "built_up_area_sqft", "bua_sqft"],
    "price": ["actual_worth", "amount", "price", "sale_price", "value", "transaction_amount", "procedure_value", "trans_value"],
    "rent": ["annual_amount", "contract_amount", "rent_value", "rent_amount", "amount", "price", "annual_rent", "contract_value"],
}


def db(url: str):
    if not url:
        raise RuntimeError("Database URL is not set")
    c = psycopg2.connect(url, cursor_factory=RealDictCursor)
    c.autocommit = False
    if STATEMENT_TIMEOUT_MS > 0:
        with c.cursor() as cur:
            cur.execute("SET statement_timeout = %s", (STATEMENT_TIMEOUT_MS,))
    return c


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def table_exists(conn, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema=%s AND table_name=%s
            ) AS ok
        """, (schema, table))
        return bool(cur.fetchone()["ok"])


def columns(conn, schema: str, table: str) -> List[str]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema=%s AND table_name=%s
            ORDER BY ordinal_position
        """, (schema, table))
        return [r["column_name"] for r in cur.fetchall()]


def pick(cols: List[str], key: str) -> Optional[str]:
    low = {c.lower(): c for c in cols}
    for cand in COLUMN_CANDIDATES.get(key, []):
        if cand.lower() in low:
            return low[cand.lower()]
    return None


def clean_text_expr(cols: List[str], key: str, fallback="'Unknown'") -> str:
    c = pick(cols, key)
    if not c:
        return fallback
    return f"COALESCE(NULLIF(INITCAP(TRIM({q(c)}::text)), ''), {fallback})"


def amount_expr(cols: List[str], key: str) -> str:
    c = pick(cols, key)
    if not c:
        return "NULL::numeric"
    return f"NULLIF(regexp_replace(COALESCE({q(c)}::text, ''), '[^0-9.]', '', 'g'), '')::numeric"


def date_expr(cols: List[str]) -> str:
    c = pick(cols, "date")
    if not c:
        return "NULL::date"
    return f"CASE WHEN NULLIF(TRIM({q(c)}::text), '') IS NULL THEN NULL::date ELSE NULLIF(TRIM({q(c)}::text), '')::date END"


def unit_segment_expr(cols: List[str]) -> str:
    rooms = clean_text_expr(cols, "rooms", "NULL")
    unit_type = clean_text_expr(cols, "unit_type", "NULL")
    raw = f"LOWER(COALESCE({rooms}, {unit_type}, 'unknown'))"
    return f"""
        CASE
            WHEN {raw} LIKE '%studio%' THEN 'Studio'
            WHEN {raw} ~ '(^|[^0-9])1([^0-9]|$)' OR {raw} LIKE '%1 br%' OR {raw} LIKE '%1 bedroom%' THEN '1BR'
            WHEN {raw} ~ '(^|[^0-9])2([^0-9]|$)' OR {raw} LIKE '%2 br%' OR {raw} LIKE '%2 bedroom%' THEN '2BR'
            WHEN {raw} ~ '(^|[^0-9])3([^0-9]|$)' OR {raw} LIKE '%3 br%' OR {raw} LIKE '%3 bedroom%' THEN '3BR'
            WHEN {raw} ~ '(^|[^0-9])4([^0-9]|$)' OR {raw} LIKE '%4 br%' OR {raw} LIKE '%4 bedroom%' THEN '4BR'
            WHEN {raw} ~ '(^|[^0-9])5([^0-9]|$)' OR {raw} LIKE '%5 br%' OR {raw} LIKE '%5 bedroom%' THEN '5BR+'
            WHEN {raw} LIKE '%penthouse%' THEN 'Penthouse'
            ELSE 'Unknown'
        END
    """


def size_sqft_expr(cols: List[str]) -> str:
    sqm = amount_expr(cols, "size_sqm")
    sqft = amount_expr(cols, "size_sqft")
    return f"CASE WHEN {sqft} IS NOT NULL AND {sqft} > 0 THEN {sqft} WHEN {sqm} IS NOT NULL AND {sqm} > 0 THEN {sqm}*10.7639 ELSE NULL::numeric END"


def aggregate_sql(cols: List[str], schema: str, table: str, kind: str, period_days: Optional[int] = None, previous: bool = False) -> str:
    value_key = "price" if kind == "sale" else "rent"
    value = amount_expr(cols, value_key)
    sqft = size_sqft_expr(cols)
    date = date_expr(cols)
    area = clean_text_expr(cols, "area")
    building = clean_text_expr(cols, "building")
    prop = clean_text_expr(cols, "property_type")
    unit = unit_segment_expr(cols)

    date_filter = ""
    if period_days:
        if previous:
            date_filter = f"AND {date} >= CURRENT_DATE - INTERVAL '{period_days*2} days' AND {date} < CURRENT_DATE - INTERVAL '{period_days} days'"
        else:
            date_filter = f"AND {date} >= CURRENT_DATE - INTERVAL '{period_days} days'"

    psf = f"CASE WHEN ({sqft}) IS NOT NULL AND ({sqft}) > 0 THEN ({value})/({sqft}) ELSE NULL::numeric END"
    return f"""
        SELECT
            {area} AS area_name,
            {building} AS building_name,
            {prop} AS property_type,
            {unit} AS unit_segment,
            COUNT(*)::bigint AS deals_count,
            AVG({value})::numeric AS avg_amount,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY {value})::numeric AS median_amount,
            AVG({psf})::numeric AS avg_psf,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY {psf})::numeric AS median_psf,
            MIN({date}) AS first_date,
            MAX({date}) AS last_date
        FROM {q(schema)}.{q(table)}
        WHERE {value} IS NOT NULL AND {value} > 0
          AND {area} IS NOT NULL
          AND {building} IS NOT NULL
          {date_filter}
        GROUP BY 1,2,3,4
    """


def source_aggregate(source: Tuple[str, str, str, str], kind: str, period_days: Optional[int] = None, previous: bool = False) -> List[dict]:
    source_name, url, schema, table = source
    if not url:
        return []
    try:
        with db(url) as c:
            if not table_exists(c, schema, table):
                logging.warning("Source table not found: %s.%s", schema, table)
                return []
            cols = columns(c, schema, table)
            sql = aggregate_sql(cols, schema, table, kind, period_days, previous)
            logging.info("Aggregating %s %s.%s%s%s...", kind, schema, table, f" {period_days}d" if period_days else "", " previous" if previous else "")
            with c.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
                logging.info("Aggregated rows from %s.%s: %s", schema, table, len(rows))
                return rows
    except Exception:
        logging.exception("Failed source aggregate %s %s.%s", kind, schema, table)
        return []


def merge_aggs(rows: List[dict]) -> Dict[Tuple[str, str, str, str], dict]:
    out: Dict[Tuple[str, str, str, str], dict] = {}
    for r in rows:
        key = (r.get("area_name") or "Unknown", r.get("building_name") or "Unknown", r.get("property_type") or "Unknown", r.get("unit_segment") or "Unknown")
        cnt = int(r.get("deals_count") or 0)
        if cnt <= 0:
            continue
        cur = out.setdefault(key, {"area_name": key[0], "building_name": key[1], "property_type": key[2], "unit_segment": key[3], "deals_count": 0, "avg_amount_sum": 0, "median_amount_sum": 0, "avg_psf_sum": 0, "median_psf_sum": 0})
        cur["deals_count"] += cnt
        for f in ["avg_amount", "median_amount", "avg_psf", "median_psf"]:
            v = r.get(f)
            if v is not None:
                cur[f + "_sum"] += float(v) * cnt
    for cur in out.values():
        cnt = cur["deals_count"] or 1
        for f in ["avg_amount", "median_amount", "avg_psf", "median_psf"]:
            cur[f] = cur.pop(f + "_sum", 0) / cnt if cnt else None
    return out


def calc_score(roi, sales, rents, psf_change=None, rent_change=None) -> float:
    roi = float(roi or 0)
    sales = int(sales or 0)
    rents = int(rents or 0)
    liquidity = min(100, (sales + rents) / 20)
    growth = max(-20, min(40, float(psf_change or 0))) + max(-20, min(40, float(rent_change or 0)))
    return max(0, min(100, roi * 7 + liquidity * 0.35 + growth * 0.5))


def economic_text(row: dict) -> str:
    roi = row.get("gross_roi_percent")
    score = row.get("investment_score")
    liq = row.get("liquidity_score")
    verdict = "strong investment candidate" if (score or 0) >= 70 else "balanced opportunity" if (score or 0) >= 50 else "requires selective entry and negotiation"
    return f"DLD-based analysis indicates {verdict}. Gross ROI is estimated at {roi:.1f}% where rent and sale benchmarks are available. Liquidity score is {liq:.0f}/100 based on transaction depth. Best use: compare asking price against median sale price and negotiate below market median when liquidity is moderate or low."


def create_tables(c):
    with c.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS building_roi_summary (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ DEFAULT now(),
            area_name TEXT, building_name TEXT, property_type TEXT, unit_segment TEXT,
            sales_count BIGINT, rents_count BIGINT,
            avg_sale_price NUMERIC, median_sale_price NUMERIC, avg_sale_psf NUMERIC, median_sale_psf NUMERIC,
            avg_rent NUMERIC, median_rent NUMERIC,
            gross_roi_percent NUMERIC, liquidity_score NUMERIC, investment_score NUMERIC,
            economic_conclusion TEXT
        );
        CREATE TABLE IF NOT EXISTS area_roi_summary (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ DEFAULT now(),
            area_name TEXT, property_type TEXT, unit_segment TEXT,
            sales_count BIGINT, rents_count BIGINT,
            avg_sale_price NUMERIC, median_sale_price NUMERIC, avg_sale_psf NUMERIC, median_sale_psf NUMERIC,
            avg_rent NUMERIC, median_rent NUMERIC,
            gross_roi_percent NUMERIC, liquidity_score NUMERIC, investment_score NUMERIC,
            economic_conclusion TEXT
        );
        CREATE TABLE IF NOT EXISTS building_period_comparison (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ DEFAULT now(),
            period_code TEXT, area_name TEXT, building_name TEXT, property_type TEXT, unit_segment TEXT,
            current_sales_count BIGINT, previous_sales_count BIGINT, current_rents_count BIGINT, previous_rents_count BIGINT,
            current_median_sale_price NUMERIC, previous_median_sale_price NUMERIC,
            current_median_sale_psf NUMERIC, previous_median_sale_psf NUMERIC,
            current_median_rent NUMERIC, previous_median_rent NUMERIC,
            current_roi_percent NUMERIC, previous_roi_percent NUMERIC,
            sale_price_change_percent NUMERIC, sale_psf_change_percent NUMERIC, rent_change_percent NUMERIC, roi_change_pp NUMERIC,
            volume_change_percent NUMERIC, momentum_score NUMERIC, trend_label TEXT, professional_conclusion TEXT
        );
        CREATE TABLE IF NOT EXISTS area_period_comparison (LIKE building_period_comparison INCLUDING ALL);
        CREATE TABLE IF NOT EXISTS market_period_summary (LIKE building_period_comparison INCLUDING ALL);
        CREATE TABLE IF NOT EXISTS investment_recommendations (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ DEFAULT now(),
            area_name TEXT, building_name TEXT, property_type TEXT, unit_segment TEXT,
            investment_score NUMERIC, gross_roi_percent NUMERIC, liquidity_score NUMERIC,
            avg_sale_price NUMERIC, median_rent NUMERIC, recommendation TEXT, reason TEXT
        );
        CREATE TABLE IF NOT EXISTS intelligence_status (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ DEFAULT now(), status TEXT, details TEXT,
            building_summaries_count BIGINT, area_summaries_count BIGINT, period_rows_count BIGINT
        );
        CREATE TABLE IF NOT EXISTS intelligence_sales (id BIGSERIAL PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT now(), area_name TEXT, building_name TEXT, property_type TEXT, unit_segment TEXT, deals_count BIGINT, median_amount NUMERIC, median_psf NUMERIC);
        CREATE TABLE IF NOT EXISTS intelligence_rents (id BIGSERIAL PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT now(), area_name TEXT, building_name TEXT, property_type TEXT, unit_segment TEXT, deals_count BIGINT, median_amount NUMERIC, median_psf NUMERIC);
        CREATE TABLE IF NOT EXISTS intelligence_roi (id BIGSERIAL PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT now(), area_name TEXT, building_name TEXT, property_type TEXT, unit_segment TEXT, roi_percent NUMERIC, investment_score NUMERIC);
        CREATE TABLE IF NOT EXISTS intelligence_market_stats (id BIGSERIAL PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT now(), area_name TEXT, property_type TEXT, unit_segment TEXT, sales_count BIGINT, rents_count BIGINT, roi_percent NUMERIC);
        CREATE TABLE IF NOT EXISTS intelligence_period_comparison (id BIGSERIAL PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT now(), period_code TEXT, area_name TEXT, building_name TEXT, property_type TEXT, unit_segment TEXT, momentum_score NUMERIC);
        CREATE INDEX IF NOT EXISTS idx_brs_lookup ON building_roi_summary (area_name, building_name, property_type, unit_segment);
        CREATE INDEX IF NOT EXISTS idx_ars_lookup ON area_roi_summary (area_name, property_type, unit_segment);
        CREATE INDEX IF NOT EXISTS idx_bpc_lookup ON building_period_comparison (period_code, area_name, building_name, property_type, unit_segment);
        CREATE INDEX IF NOT EXISTS idx_apc_lookup ON area_period_comparison (period_code, area_name, property_type, unit_segment);
        """)
    c.commit()


def pct(new, old):
    if new is None or old is None or float(old) == 0:
        return None
    return (float(new) - float(old)) / float(old) * 100


def build_rows(sales: Dict, rents: Dict) -> List[dict]:
    keys = set(sales) | set(rents)
    rows = []
    for key in keys:
        s, r = sales.get(key, {}), rents.get(key, {})
        median_sale = s.get("median_amount")
        median_rent = r.get("median_amount")
        roi = (float(median_rent) / float(median_sale) * 100) if median_sale and median_rent else None
        sales_count = int(s.get("deals_count") or 0)
        rents_count = int(r.get("deals_count") or 0)
        liquidity = min(100, (sales_count + rents_count) / 20)
        score = calc_score(roi, sales_count, rents_count)
        row = {
            "area_name": key[0], "building_name": key[1], "property_type": key[2], "unit_segment": key[3],
            "sales_count": sales_count, "rents_count": rents_count,
            "avg_sale_price": s.get("avg_amount"), "median_sale_price": median_sale,
            "avg_sale_psf": s.get("avg_psf"), "median_sale_psf": s.get("median_psf"),
            "avg_rent": r.get("avg_amount"), "median_rent": median_rent,
            "gross_roi_percent": roi, "liquidity_score": liquidity, "investment_score": score,
        }
        row["economic_conclusion"] = economic_text({**row, "gross_roi_percent": roi or 0})
        rows.append(row)
    return rows


def area_rows_from_buildings(buildings: List[dict]) -> List[dict]:
    groups: Dict[Tuple[str, str, str], dict] = {}
    for b in buildings:
        key = (b["area_name"], b["property_type"], b["unit_segment"])
        g = groups.setdefault(key, {"items": [], "sales_count": 0, "rents_count": 0})
        g["items"].append(b)
        g["sales_count"] += b.get("sales_count") or 0
        g["rents_count"] += b.get("rents_count") or 0
    rows = []
    for key, g in groups.items():
        items = g["items"]
        def wavg(field):
            vals = [(x.get(field), (x.get("sales_count") or x.get("rents_count") or 1)) for x in items if x.get(field) is not None]
            if not vals: return None
            return sum(float(v)*w for v,w in vals) / sum(w for _,w in vals)
        median_sale = wavg("median_sale_price")
        median_rent = wavg("median_rent")
        roi = (median_rent / median_sale * 100) if median_sale and median_rent else None
        liquidity = min(100, (g["sales_count"] + g["rents_count"]) / 40)
        score = calc_score(roi, g["sales_count"], g["rents_count"])
        row = {
            "area_name": key[0], "property_type": key[1], "unit_segment": key[2],
            "sales_count": g["sales_count"], "rents_count": g["rents_count"],
            "avg_sale_price": wavg("avg_sale_price"), "median_sale_price": median_sale,
            "avg_sale_psf": wavg("avg_sale_psf"), "median_sale_psf": wavg("median_sale_psf"),
            "avg_rent": wavg("avg_rent"), "median_rent": median_rent,
            "gross_roi_percent": roi, "liquidity_score": liquidity, "investment_score": score,
        }
        row["economic_conclusion"] = economic_text({**row, "building_name": "area", "gross_roi_percent": roi or 0})
        rows.append(row)
    return rows


def period_rows(current_sales: Dict, prev_sales: Dict, current_rents: Dict, prev_rents: Dict, period_code: str, scope: str) -> List[dict]:
    keys = set(current_sales) | set(prev_sales) | set(current_rents) | set(prev_rents)
    rows = []
    for key in keys:
        cs, ps, cr, pr = current_sales.get(key, {}), prev_sales.get(key, {}), current_rents.get(key, {}), prev_rents.get(key, {})
        cur_roi = (float(cr.get("median_amount")) / float(cs.get("median_amount")) * 100) if cr.get("median_amount") and cs.get("median_amount") else None
        prev_roi = (float(pr.get("median_amount")) / float(ps.get("median_amount")) * 100) if pr.get("median_amount") and ps.get("median_amount") else None
        price_ch = pct(cs.get("median_amount"), ps.get("median_amount"))
        psf_ch = pct(cs.get("median_psf"), ps.get("median_psf"))
        rent_ch = pct(cr.get("median_amount"), pr.get("median_amount"))
        vol_cur = int(cs.get("deals_count") or 0) + int(cr.get("deals_count") or 0)
        vol_prev = int(ps.get("deals_count") or 0) + int(pr.get("deals_count") or 0)
        vol_ch = pct(vol_cur, vol_prev)
        roi_ch = (cur_roi - prev_roi) if cur_roi is not None and prev_roi is not None else None
        momentum = max(0, min(100, 50 + (price_ch or 0)*0.6 + (rent_ch or 0)*0.4 + (vol_ch or 0)*0.2 + (roi_ch or 0)*2))
        trend = "STRONG UP" if momentum >= 70 else "STABLE / POSITIVE" if momentum >= 50 else "WEAK / NEGATIVE"
        row = {
            "period_code": period_code, "area_name": key[0], "building_name": key[1], "property_type": key[2], "unit_segment": key[3],
            "current_sales_count": int(cs.get("deals_count") or 0), "previous_sales_count": int(ps.get("deals_count") or 0),
            "current_rents_count": int(cr.get("deals_count") or 0), "previous_rents_count": int(pr.get("deals_count") or 0),
            "current_median_sale_price": cs.get("median_amount"), "previous_median_sale_price": ps.get("median_amount"),
            "current_median_sale_psf": cs.get("median_psf"), "previous_median_sale_psf": ps.get("median_psf"),
            "current_median_rent": cr.get("median_amount"), "previous_median_rent": pr.get("median_amount"),
            "current_roi_percent": cur_roi, "previous_roi_percent": prev_roi,
            "sale_price_change_percent": price_ch, "sale_psf_change_percent": psf_ch, "rent_change_percent": rent_ch, "roi_change_pp": roi_ch,
            "volume_change_percent": vol_ch, "momentum_score": momentum, "trend_label": trend,
            "professional_conclusion": f"{period_code} trend is {trend}. DLD momentum score is {momentum:.0f}/100 based on price, rent, ROI and volume changes."
        }
        rows.append(row)
    return rows


def truncate_tables(c):
    with c.cursor() as cur:
        cur.execute("""
            TRUNCATE building_roi_summary, area_roi_summary, building_period_comparison, area_period_comparison,
            market_period_summary, investment_recommendations, intelligence_sales, intelligence_rents,
            intelligence_roi, intelligence_market_stats, intelligence_period_comparison RESTART IDENTITY;
        """)
    c.commit()


def insert_buildings(c, rows: List[dict]):
    if not rows: return
    vals = [[r.get(k) for k in ["area_name","building_name","property_type","unit_segment","sales_count","rents_count","avg_sale_price","median_sale_price","avg_sale_psf","median_sale_psf","avg_rent","median_rent","gross_roi_percent","liquidity_score","investment_score","economic_conclusion"]] for r in rows]
    with c.cursor() as cur:
        execute_values(cur, """INSERT INTO building_roi_summary (area_name,building_name,property_type,unit_segment,sales_count,rents_count,avg_sale_price,median_sale_price,avg_sale_psf,median_sale_psf,avg_rent,median_rent,gross_roi_percent,liquidity_score,investment_score,economic_conclusion) VALUES %s""", vals, page_size=5000)
        execute_values(cur, """INSERT INTO intelligence_sales (area_name,building_name,property_type,unit_segment,deals_count,median_amount,median_psf) VALUES %s""", [[r["area_name"],r["building_name"],r["property_type"],r["unit_segment"],r["sales_count"],r["median_sale_price"],r["median_sale_psf"]] for r in rows], page_size=5000)
        execute_values(cur, """INSERT INTO intelligence_rents (area_name,building_name,property_type,unit_segment,deals_count,median_amount,median_psf) VALUES %s""", [[r["area_name"],r["building_name"],r["property_type"],r["unit_segment"],r["rents_count"],r["median_rent"],None] for r in rows], page_size=5000)
        execute_values(cur, """INSERT INTO intelligence_roi (area_name,building_name,property_type,unit_segment,roi_percent,investment_score) VALUES %s""", [[r["area_name"],r["building_name"],r["property_type"],r["unit_segment"],r["gross_roi_percent"],r["investment_score"]] for r in rows], page_size=5000)
    c.commit()


def insert_areas(c, rows: List[dict]):
    if not rows: return
    vals = [[r.get(k) for k in ["area_name","property_type","unit_segment","sales_count","rents_count","avg_sale_price","median_sale_price","avg_sale_psf","median_sale_psf","avg_rent","median_rent","gross_roi_percent","liquidity_score","investment_score","economic_conclusion"]] for r in rows]
    with c.cursor() as cur:
        execute_values(cur, """INSERT INTO area_roi_summary (area_name,property_type,unit_segment,sales_count,rents_count,avg_sale_price,median_sale_price,avg_sale_psf,median_sale_psf,avg_rent,median_rent,gross_roi_percent,liquidity_score,investment_score,economic_conclusion) VALUES %s""", vals, page_size=5000)
        execute_values(cur, """INSERT INTO intelligence_market_stats (area_name,property_type,unit_segment,sales_count,rents_count,roi_percent) VALUES %s""", [[r["area_name"],r["property_type"],r["unit_segment"],r["sales_count"],r["rents_count"],r["gross_roi_percent"]] for r in rows], page_size=5000)
    c.commit()


def insert_period(c, table: str, rows: List[dict]):
    if not rows: return
    keys = ["period_code","area_name","building_name","property_type","unit_segment","current_sales_count","previous_sales_count","current_rents_count","previous_rents_count","current_median_sale_price","previous_median_sale_price","current_median_sale_psf","previous_median_sale_psf","current_median_rent","previous_median_rent","current_roi_percent","previous_roi_percent","sale_price_change_percent","sale_psf_change_percent","rent_change_percent","roi_change_pp","volume_change_percent","momentum_score","trend_label","professional_conclusion"]
    vals = [[r.get(k) for k in keys] for r in rows]
    with c.cursor() as cur:
        execute_values(cur, f"""INSERT INTO {table} ({','.join(keys)}) VALUES %s""", vals, page_size=5000)
        if table == "building_period_comparison":
            execute_values(cur, """INSERT INTO intelligence_period_comparison (period_code,area_name,building_name,property_type,unit_segment,momentum_score) VALUES %s""", [[r["period_code"],r["area_name"],r["building_name"],r["property_type"],r["unit_segment"],r["momentum_score"]] for r in rows], page_size=5000)
    c.commit()


def insert_recommendations(c, rows: List[dict]):
    rows = sorted(rows, key=lambda r: float(r.get("investment_score") or 0), reverse=True)[:500]
    vals = [[r["area_name"],r["building_name"],r["property_type"],r["unit_segment"],r["investment_score"],r["gross_roi_percent"],r["liquidity_score"],r["avg_sale_price"],r["median_rent"],"Recommended based on DLD ROI, liquidity and market depth.",r["economic_conclusion"]] for r in rows]
    if not vals: return
    with c.cursor() as cur:
        execute_values(cur, """INSERT INTO investment_recommendations (area_name,building_name,property_type,unit_segment,investment_score,gross_roi_percent,liquidity_score,avg_sale_price,median_rent,recommendation,reason) VALUES %s""", vals, page_size=1000)
    c.commit()


def run_cycle():
    logging.info("="*80)
    logging.info("Dubai DLD Professional Intelligence Engine v4 production started")
    logging.info("Mode: archive + live + intelligence; SQL aggregation; no full raw loading")
    logging.info("="*80)
    with db(INTELLIGENCE_DATABASE_URL) as ic:
        create_tables(ic)
        truncate_tables(ic)

    sale_rows = []
    rent_rows = []
    for s in SALE_SOURCES:
        sale_rows.extend(source_aggregate(s, "sale"))
    for s in RENT_SOURCES:
        rent_rows.extend(source_aggregate(s, "rent"))

    sales = merge_aggs(sale_rows)
    rents = merge_aggs(rent_rows)
    buildings = build_rows(sales, rents)
    areas = area_rows_from_buildings(buildings)
    logging.info("Final building rows: %s | area rows: %s", len(buildings), len(areas))

    period_all = []
    for code, days in PERIODS.items():
        cs_rows, ps_rows, cr_rows, pr_rows = [], [], [], []
        for s in SALE_SOURCES:
            cs_rows.extend(source_aggregate(s, "sale", days, False))
            ps_rows.extend(source_aggregate(s, "sale", days, True))
        for s in RENT_SOURCES:
            cr_rows.extend(source_aggregate(s, "rent", days, False))
            pr_rows.extend(source_aggregate(s, "rent", days, True))
        period_all.extend(period_rows(merge_aggs(cs_rows), merge_aggs(ps_rows), merge_aggs(cr_rows), merge_aggs(pr_rows), code, "building"))

    with db(INTELLIGENCE_DATABASE_URL) as ic:
        insert_buildings(ic, buildings)
        insert_areas(ic, areas)
        insert_period(ic, "building_period_comparison", period_all)
        # area period from building rows grouped at area level: reuse rows with building as 'Market'
        area_period = []
        for r in period_all:
            rr = dict(r); rr["building_name"] = "Area Summary"; area_period.append(rr)
        insert_period(ic, "area_period_comparison", area_period)
        market_period = []
        for r in period_all[:200]:
            rr = dict(r); rr["area_name"] = "Dubai"; rr["building_name"] = "Market Summary"; market_period.append(rr)
        insert_period(ic, "market_period_summary", market_period)
        insert_recommendations(ic, buildings)
        with ic.cursor() as cur:
            cur.execute("""INSERT INTO intelligence_status (status, details, building_summaries_count, area_summaries_count, period_rows_count) VALUES (%s,%s,%s,%s,%s)""", ("ok", "v4 production SQL aggregation completed", len(buildings), len(areas), len(period_all)))
        ic.commit()
    logging.info("Intelligence status updated successfully")


def main():
    while True:
        # Cron heartbeat (best-effort; never blocks cycle)
        try:
            from auto_audit._common import record_metric
            record_metric("cron.tick.intelligence_updater", 1.0, meta={"status": "ok"})
        except Exception:
            pass
        try:
            run_cycle()
        except Exception:
            logging.exception("Intelligence cycle failed")
            try:
                with db(INTELLIGENCE_DATABASE_URL) as ic:
                    with ic.cursor() as cur:
                        cur.execute("INSERT INTO intelligence_status (status, details) VALUES (%s,%s)", ("error", "cycle failed; see Railway logs"))
                    ic.commit()
            except Exception:
                pass
        # Also refresh daily_market_reports (separate table consumed by PDF/analytics).
        try:
            import daily_reports
            daily_reports.run_daily(
                max_areas=int(os.getenv("DAILY_REPORTS_MAX_AREAS", "150")),
                max_buildings=int(os.getenv("DAILY_REPORTS_MAX_BUILDINGS", "300")),
            )
        except Exception:
            logging.exception("daily_reports.run_daily failed")
        logging.info("Sleeping %s seconds before next intelligence cycle...", CYCLE_SECONDS)
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    main()
