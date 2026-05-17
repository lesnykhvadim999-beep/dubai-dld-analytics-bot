import os
import time
import math
import logging
from datetime import datetime, UTC, timedelta
from typing import Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

LIVE_DATABASE_URL = os.getenv("LIVE_DATABASE_URL")
ARCHIVE_DATABASE_URL = os.getenv("ARCHIVE_DATABASE_URL")
INTELLIGENCE_DATABASE_URL = os.getenv("INTELLIGENCE_DATABASE_URL")

CYCLE_SECONDS = int(os.getenv("INTELLIGENCE_CYCLE_SECONDS", "21600"))

SALE_TABLES = [
    ("archive", "public", "dld_sale_archive"),
    ("live", "public", "dld_transactions_full"),
]

RENT_TABLES = [
    ("archive", "public", "dld_rent_archive"),
    ("live", "public", "dld_rents_full"),
]

PERIOD_WINDOWS = {
    "30d": 30,
    "90d": 90,
    "180d": 180,
    "365d": 365,
}

COLUMN_CANDIDATES = {
    "date": [
        "transaction_date",
        "instance_date",
        "procedure_date",
        "date",
        "registration_date",
        "contract_start_date",
        "contract_end_date",
        "ejari_contract_start_date",
    ],
    "area": [
        "area_name_en",
        "area_en",
        "area_name",
        "area",
        "master_project_en",
        "master_project",
        "project_area",
        "community",
    ],
    "building": [
        "building_name_en",
        "building_en",
        "building_name",
        "building",
        "project_name_en",
        "project_en",
        "project_name",
        "project",
        "master_project_en",
        "property_name",
    ],
    "project": [
        "project_name_en",
        "project_en",
        "project_name",
        "project",
        "master_project_en",
        "master_project",
    ],
    "property_type": [
        "property_type_en",
        "prop_type_en",
        "property_type",
        "property_usage_en",
        "property_usage",
        "usage_en",
    ],
    "unit_type": [
        "property_sub_type_en",
        "prop_sub_type_en",
        "property_sub_type",
        "unit_type_en",
        "unit_type",
        "property_type_en",
        "prop_type_en",
        "property_type",
    ],
    "rooms": [
        "rooms_en",
        "rooms",
        "rooms_ar",
        "bedrooms",
        "bedroom",
        "beds",
        "room",
    ],
    "unit": [
        "unit_number",
        "unit_no",
        "unit",
        "property_number",
        "property_no",
        "property_id",
        "property_number_en",
        "unit_number_en",
    ],
    "size_sqm": [
        "actual_area",
        "procedure_area",
        "area_sqm",
        "property_size_sqm",
        "size_sqm",
        "property_area",
        "area",
    ],
    "size_sqft": [
        "area_sqft",
        "property_size_sqft",
        "size_sqft",
        "built_up_area_sqft",
        "bua_sqft",
    ],
    "price": [
        "actual_worth",
        "amount",
        "price",
        "sale_price",
        "value",
        "transaction_amount",
        "procedure_value",
        "trans_value",
    ],
    "rent": [
        "annual_amount",
        "contract_amount",
        "rent_value",
        "rent_amount",
        "amount",
        "price",
        "annual_rent",
        "contract_value",
    ],
    "procedure": [
        "procedure_name_en",
        "procedure_name",
        "procedure",
        "transaction_type_en",
        "transaction_type",
        "reg_type_en",
    ],
    "transaction_id": [
        "transaction_id",
        "transaction_number",
        "contract_id",
        "id",
    ],
    "parking": [
        "has_parking",
        "parking",
    ],
    "freehold": [
        "is_free_hold",
        "freehold",
    ],
    "offplan": [
        "is_offplan",
        "offplan",
    ],
    "nearest_metro": [
        "nearest_metro_en",
        "nearest_metro",
    ],
    "nearest_mall": [
        "nearest_mall_en",
        "nearest_mall",
    ],
    "nearest_landmark": [
        "nearest_landmark_en",
        "nearest_landmark",
    ],
}


def require_env() -> None:
    missing = []
    for key, value in {
        "LIVE_DATABASE_URL": LIVE_DATABASE_URL,
        "ARCHIVE_DATABASE_URL": ARCHIVE_DATABASE_URL,
        "INTELLIGENCE_DATABASE_URL": INTELLIGENCE_DATABASE_URL,
    }.items():
        if not value:
            missing.append(key)

    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")


def connect(url: str):
    return psycopg2.connect(url, cursor_factory=RealDictCursor)


def has_table(conn, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_name = %s
            ) AS exists
            """,
            (schema, table),
        )
        return bool(cur.fetchone()["exists"])


def get_columns(conn, schema: str, table: str) -> List[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table),
        )
        return [r["column_name"] for r in cur.fetchall()]


def pick_col(columns: List[str], logical: str) -> Optional[str]:
    lower_map = {c.lower(): c for c in columns}

    for candidate in COLUMN_CANDIDATES.get(logical, []):
        if candidate.lower() in lower_map:
            return lower_map[candidate.lower()]

    return None


def sql_expr(columns: List[str], logical: str, fallback: str = "NULL") -> str:
    col = pick_col(columns, logical)
    if not col:
        return fallback
    return f'"{col}"'


def numeric_expr(columns: List[str], logical: str, fallback: str = "NULL") -> str:
    col = pick_col(columns, logical)
    if not col:
        return fallback

    return (
        f"NULLIF("
        f"REGEXP_REPLACE(\"{col}\"::text, '[^0-9.]', '', 'g'), "
        f"''"
        f")::numeric"
    )


def date_expr(columns: List[str]) -> str:
    col = pick_col(columns, "date")
    if not col:
        return "NULL::date"

    return f"""
        CASE
            WHEN NULLIF(TRIM("{col}"::text), '') IS NULL THEN NULL::date
            ELSE NULLIF(TRIM("{col}"::text), '')::date
        END
    """


def text_norm_sql(expr: str) -> str:
    return f"NULLIF(TRIM(COALESCE({expr}::text, '')), '')"


def boolean_text_sql(columns: List[str], logical: str) -> str:
    col = pick_col(columns, logical)
    if not col:
        return "NULL::text"
    return f"NULLIF(TRIM(\"{col}\"::text), '')"


def normalized_source_sql(
    conn,
    source_name: str,
    schema: str,
    table: str,
    deal_type: str,
) -> Optional[str]:
    if not has_table(conn, schema, table):
        logging.warning("Table missing: %s.%s", schema, table)
        return None

    columns = get_columns(conn, schema, table)
    amount_logical = "price" if deal_type == "sale" else "rent"

    size_sqm_expr = numeric_expr(columns, "size_sqm")
    size_sqft_expr = numeric_expr(columns, "size_sqft")
    amount_expr = numeric_expr(columns, amount_logical)

    size_sqft_final = f"""
        CASE
            WHEN {size_sqft_expr} IS NOT NULL AND {size_sqft_expr} > 0 THEN {size_sqft_expr}
            WHEN {size_sqm_expr} IS NOT NULL AND {size_sqm_expr} > 0 THEN {size_sqm_expr} * 10.7639
            ELSE NULL
        END
    """

    return f"""
        SELECT
            '{source_name}'::text AS source_db,
            '{deal_type}'::text AS deal_type,
            {date_expr(columns)} AS deal_date,
            {text_norm_sql(sql_expr(columns, "area", "NULL"))} AS area_name,
            {text_norm_sql(sql_expr(columns, "building", "NULL"))} AS building_name,
            {text_norm_sql(sql_expr(columns, "project", "NULL"))} AS project_name,
            {text_norm_sql(sql_expr(columns, "property_type", "NULL"))} AS property_type,
            {text_norm_sql(sql_expr(columns, "unit_type", "NULL"))} AS unit_type,
            {text_norm_sql(sql_expr(columns, "rooms", "NULL"))} AS rooms,
            {text_norm_sql(sql_expr(columns, "unit", "NULL"))} AS unit_number,
            ({size_sqm_expr})::numeric AS size_sqm,
            ({size_sqft_final})::numeric AS size_sqft,
            ({amount_expr})::numeric AS amount,
            {text_norm_sql(sql_expr(columns, "procedure", "NULL"))} AS procedure_name,
            {text_norm_sql(sql_expr(columns, "transaction_id", "NULL"))} AS source_transaction_id,
            {boolean_text_sql(columns, "parking")} AS parking,
            {boolean_text_sql(columns, "freehold")} AS freehold,
            {boolean_text_sql(columns, "offplan")} AS offplan,
            {text_norm_sql(sql_expr(columns, "nearest_metro", "NULL"))} AS nearest_metro,
            {text_norm_sql(sql_expr(columns, "nearest_mall", "NULL"))} AS nearest_mall,
            {text_norm_sql(sql_expr(columns, "nearest_landmark", "NULL"))} AS nearest_landmark
        FROM "{schema}"."{table}"
        WHERE ({amount_expr}) IS NOT NULL
          AND ({amount_expr}) > 0
    """


def fetch_normalized_deals(
    conn,
    tables: List[Tuple[str, str, str]],
    deal_type: str,
) -> List[dict]:
    parts = []

    for source_name, schema, table in tables:
        part = normalized_source_sql(conn, source_name, schema, table, deal_type)
        if part:
            parts.append(part)

    if not parts:
        return []

    sql = " UNION ALL ".join(parts)

    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def safe_float(value) -> Optional[float]:
    try:
        if value is None:
            return None
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    except Exception:
        return None


def median(values: List[Optional[float]]) -> Optional[float]:
    clean = sorted([v for v in values if v is not None and v > 0])
    if not clean:
        return None
    n = len(clean)
    mid = n // 2
    if n % 2:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) / 2


def avg(values: List[Optional[float]]) -> Optional[float]:
    clean = [v for v in values if v is not None and v > 0]
    if not clean:
        return None
    return sum(clean) / len(clean)


def pct_change(new: Optional[float], old: Optional[float]) -> Optional[float]:
    if new is None or old is None or old == 0:
        return None
    return (new - old) / old * 100


def score_0_100(value: Optional[float], low: float, high: float) -> Optional[float]:
    if value is None:
        return None
    if high == low:
        return 50
    return max(0, min(100, (value - low) / (high - low) * 100))


def normalize_text(value: Optional[str]) -> str:
    if not value:
        return "Unknown"

    clean = " ".join(str(value).strip().split())

    if not clean or clean in {"-", "--", "N/A", "n/a", "None", "none", "null", "NULL"}:
        return "Unknown"

    return clean


def normalize_property_type(value: Optional[str]) -> str:
    v = normalize_text(value).lower()

    if any(x in v for x in ["villa"]):
        return "Villa"
    if any(x in v for x in ["townhouse", "town house"]):
        return "Townhouse"
    if any(x in v for x in ["office", "shop", "retail", "commercial", "warehouse"]):
        return "Commercial"
    if any(x in v for x in ["plot", "land"]):
        return "Plot"
    if any(x in v for x in ["apartment", "flat", "unit", "residential"]):
        return "Apartment"

    return normalize_text(value)


def normalize_segment(rooms: Optional[str], unit_type: Optional[str], property_type: Optional[str]) -> str:
    raw = f"{rooms or ''} {unit_type or ''} {property_type or ''}".lower().strip()

    if "studio" in raw:
        return "Studio"

    for n in range(1, 11):
        variants = [
            f"{n} br",
            f"{n}br",
            f"{n} bedroom",
            f"{n} bedrooms",
            f"{n}-bed",
            f"{n} bed",
            f"{n} beds",
        ]
        if any(v in raw for v in variants):
            return f"{n}BR"
        if raw == str(n):
            return f"{n}BR"

    if "penthouse" in raw:
        return "Penthouse"
    if "office" in raw:
        return "Office"
    if "shop" in raw or "retail" in raw:
        return "Retail"
    if "plot" in raw or "land" in raw:
        return "Plot"

    return normalize_text(rooms or unit_type or property_type)


def key_for(deal: dict) -> Tuple[str, str, str, str]:
    property_type = normalize_property_type(
        deal.get("property_type")
        or deal.get("unit_type")
        or deal.get("project_name")
    )

    return (
        normalize_text(deal.get("area_name")),
        normalize_text(deal.get("building_name")),
        property_type,
        normalize_segment(deal.get("rooms"), deal.get("unit_type"), property_type),
    )


def area_key_for(deal: dict) -> Tuple[str, str, str]:
    area, _building, property_type, segment = key_for(deal)
    return area, property_type, segment


def create_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS investment_recommendations")
        cur.execute("DROP TABLE IF EXISTS market_period_summary")
        cur.execute("DROP TABLE IF EXISTS area_period_comparison")
        cur.execute("DROP TABLE IF EXISTS building_period_comparison")
        cur.execute("DROP TABLE IF EXISTS area_roi_summary")
        cur.execute("DROP TABLE IF EXISTS building_roi_summary")
        cur.execute("DROP TABLE IF EXISTS intelligence_status")

        cur.execute("""
            CREATE TABLE building_roi_summary (
                id BIGSERIAL PRIMARY KEY,
                calculated_at TIMESTAMP NOT NULL,
                area_name TEXT,
                building_name TEXT,
                property_type TEXT,
                unit_segment TEXT,
                sales_count INTEGER,
                rents_count INTEGER,
                avg_sale_price NUMERIC,
                median_sale_price NUMERIC,
                avg_sale_psf NUMERIC,
                median_sale_psf NUMERIC,
                avg_rent NUMERIC,
                median_rent NUMERIC,
                avg_rent_psf NUMERIC,
                median_rent_psf NUMERIC,
                gross_roi_percent NUMERIC,
                liquidity_score NUMERIC,
                yield_score NUMERIC,
                price_score NUMERIC,
                investment_score NUMERIC,
                price_position TEXT,
                recommendation TEXT,
                economic_conclusion TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE area_roi_summary (
                id BIGSERIAL PRIMARY KEY,
                calculated_at TIMESTAMP NOT NULL,
                area_name TEXT,
                property_type TEXT,
                unit_segment TEXT,
                sales_count INTEGER,
                rents_count INTEGER,
                avg_sale_price NUMERIC,
                median_sale_price NUMERIC,
                avg_sale_psf NUMERIC,
                median_sale_psf NUMERIC,
                avg_rent NUMERIC,
                median_rent NUMERIC,
                gross_roi_percent NUMERIC,
                liquidity_score NUMERIC,
                investment_score NUMERIC,
                recommendation TEXT,
                economic_conclusion TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE building_period_comparison (
                id BIGSERIAL PRIMARY KEY,
                calculated_at TIMESTAMP NOT NULL,
                period_code TEXT,
                current_days INTEGER,
                previous_days INTEGER,
                area_name TEXT,
                building_name TEXT,
                property_type TEXT,
                unit_segment TEXT,
                current_sales_count INTEGER,
                previous_sales_count INTEGER,
                current_rents_count INTEGER,
                previous_rents_count INTEGER,
                current_median_sale_price NUMERIC,
                previous_median_sale_price NUMERIC,
                sale_price_change_percent NUMERIC,
                current_median_sale_psf NUMERIC,
                previous_median_sale_psf NUMERIC,
                sale_psf_change_percent NUMERIC,
                current_median_rent NUMERIC,
                previous_median_rent NUMERIC,
                rent_change_percent NUMERIC,
                current_roi_percent NUMERIC,
                previous_roi_percent NUMERIC,
                roi_change_pp NUMERIC,
                volume_change_percent NUMERIC,
                momentum_score NUMERIC,
                trend_label TEXT,
                professional_conclusion TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE area_period_comparison (
                id BIGSERIAL PRIMARY KEY,
                calculated_at TIMESTAMP NOT NULL,
                period_code TEXT,
                current_days INTEGER,
                previous_days INTEGER,
                area_name TEXT,
                property_type TEXT,
                unit_segment TEXT,
                current_sales_count INTEGER,
                previous_sales_count INTEGER,
                current_rents_count INTEGER,
                previous_rents_count INTEGER,
                current_median_sale_price NUMERIC,
                previous_median_sale_price NUMERIC,
                sale_price_change_percent NUMERIC,
                current_median_sale_psf NUMERIC,
                previous_median_sale_psf NUMERIC,
                sale_psf_change_percent NUMERIC,
                current_median_rent NUMERIC,
                previous_median_rent NUMERIC,
                rent_change_percent NUMERIC,
                current_roi_percent NUMERIC,
                previous_roi_percent NUMERIC,
                roi_change_pp NUMERIC,
                volume_change_percent NUMERIC,
                momentum_score NUMERIC,
                trend_label TEXT,
                professional_conclusion TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE market_period_summary (
                id BIGSERIAL PRIMARY KEY,
                calculated_at TIMESTAMP NOT NULL,
                period_code TEXT,
                current_days INTEGER,
                previous_days INTEGER,
                property_type TEXT,
                unit_segment TEXT,
                current_sales_count INTEGER,
                previous_sales_count INTEGER,
                current_rents_count INTEGER,
                previous_rents_count INTEGER,
                current_median_sale_price NUMERIC,
                previous_median_sale_price NUMERIC,
                sale_price_change_percent NUMERIC,
                current_median_sale_psf NUMERIC,
                previous_median_sale_psf NUMERIC,
                sale_psf_change_percent NUMERIC,
                current_median_rent NUMERIC,
                previous_median_rent NUMERIC,
                rent_change_percent NUMERIC,
                current_roi_percent NUMERIC,
                previous_roi_percent NUMERIC,
                roi_change_pp NUMERIC,
                volume_change_percent NUMERIC,
                market_temperature TEXT,
                professional_conclusion TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE investment_recommendations (
                id BIGSERIAL PRIMARY KEY,
                calculated_at TIMESTAMP NOT NULL,
                area_name TEXT,
                building_name TEXT,
                property_type TEXT,
                unit_segment TEXT,
                investment_score NUMERIC,
                gross_roi_percent NUMERIC,
                liquidity_score NUMERIC,
                avg_sale_price NUMERIC,
                median_rent NUMERIC,
                recommendation TEXT,
                reason TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE intelligence_status (
                id BIGSERIAL PRIMARY KEY,
                created_at TIMESTAMP NOT NULL,
                live_sales_count BIGINT,
                live_rents_count BIGINT,
                archive_sales_count BIGINT,
                archive_rents_count BIGINT,
                building_summaries_count BIGINT,
                area_summaries_count BIGINT,
                building_period_rows_count BIGINT,
                area_period_rows_count BIGINT,
                market_period_rows_count BIGINT,
                status TEXT
            )
        """)

        cur.execute("CREATE INDEX idx_building_roi_lookup ON building_roi_summary (area_name, building_name, property_type, unit_segment)")
        cur.execute("CREATE INDEX idx_building_roi_score ON building_roi_summary (investment_score DESC)")
        cur.execute("CREATE INDEX idx_area_roi_lookup ON area_roi_summary (area_name, property_type, unit_segment)")
        cur.execute("CREATE INDEX idx_building_period_lookup ON building_period_comparison (period_code, area_name, building_name, property_type, unit_segment)")
        cur.execute("CREATE INDEX idx_building_period_momentum ON building_period_comparison (period_code, momentum_score DESC)")
        cur.execute("CREATE INDEX idx_area_period_lookup ON area_period_comparison (period_code, area_name, property_type, unit_segment)")
        cur.execute("CREATE INDEX idx_area_period_momentum ON area_period_comparison (period_code, momentum_score DESC)")
        cur.execute("CREATE INDEX idx_market_period_lookup ON market_period_summary (period_code, property_type, unit_segment)")
        cur.execute("CREATE INDEX idx_recommendations_score ON investment_recommendations (investment_score DESC)")

    conn.commit()


def economic_conclusion(
    area: str,
    building: str,
    property_type: str,
    segment: str,
    sales_count: int,
    rents_count: int,
    median_sale_price: Optional[float],
    median_sale_psf: Optional[float],
    median_rent: Optional[float],
    roi: Optional[float],
    liquidity_score: Optional[float],
    investment_score: Optional[float],
) -> Tuple[str, str]:
    if roi is None or not median_sale_price or not median_rent:
        recommendation = "WATCH"
        text = (
            f"{building}, {area}. Сегмент: {property_type}, {segment}. "
            f"Недостаточно стабильных сопоставимых данных для точного ROI: продаж {sales_count}, аренды {rents_count}. "
            f"Вывод: объект можно держать в watchlist, но для инвестиционного решения нужны дополнительные comparables."
        )
        return recommendation, text

    if investment_score is not None and investment_score >= 75:
        recommendation = "BUY"
    elif investment_score is not None and investment_score >= 55:
        recommendation = "SELECTIVE BUY / HOLD"
    else:
        recommendation = "NEGOTIATE / AVOID"

    if roi >= 8:
        roi_text = "высокая валовая доходность"
    elif roi >= 6:
        roi_text = "здоровая валовая доходность"
    elif roi >= 4:
        roi_text = "умеренная валовая доходность"
    else:
        roi_text = "низкая валовая доходность"

    breakeven_years = median_sale_price / median_rent if median_rent else None

    text = (
        f"{building}, {area}. Сегмент: {property_type}, {segment}. "
        f"Медианная цена покупки: {median_sale_price:,.0f} AED. "
        f"Медианная цена за sqft: {median_sale_psf or 0:,.0f} AED. "
        f"Медианная годовая аренда: {median_rent:,.0f} AED. "
        f"Gross ROI: {roi:.2f}% — {roi_text}. "
        f"Ориентир валового дохода: 1 год ≈ {median_rent:,.0f} AED, "
        f"3 года ≈ {median_rent * 3:,.0f} AED, "
        f"6 лет ≈ {median_rent * 6:,.0f} AED. "
        f"Ориентировочная окупаемость по gross rent: {breakeven_years or 0:.1f} лет. "
        f"Ликвидность: {liquidity_score or 0:.0f}/100. "
        f"Инвестиционный скоринг: {investment_score or 0:.0f}/100. "
        f"Рекомендация: {recommendation}. "
        f"Расчёт основан на {sales_count} сделках продаж и {rents_count} арендных контрактах по сопоставимому сегменту."
    )

    return recommendation, text


def calculate_building_summary(sales: List[dict], rents: List[dict]) -> List[dict]:
    grouped: Dict[Tuple[str, str, str, str], Dict[str, List[dict]]] = {}

    for deal in sales:
        grouped.setdefault(key_for(deal), {"sales": [], "rents": []})["sales"].append(deal)

    for deal in rents:
        grouped.setdefault(key_for(deal), {"sales": [], "rents": []})["rents"].append(deal)

    rows = []
    now = datetime.now(UTC).replace(tzinfo=None)

    for (area, building, property_type, segment), data in grouped.items():
        s = data["sales"]
        r = data["rents"]

        if len(s) < 3 and len(r) < 3:
            continue

        sale_prices = [safe_float(x.get("amount")) for x in s]
        sale_psf = [
            safe_float(x.get("amount")) / safe_float(x.get("size_sqft"))
            for x in s
            if safe_float(x.get("amount")) and safe_float(x.get("size_sqft"))
        ]

        rent_amounts = [safe_float(x.get("amount")) for x in r]
        rent_psf = [
            safe_float(x.get("amount")) / safe_float(x.get("size_sqft"))
            for x in r
            if safe_float(x.get("amount")) and safe_float(x.get("size_sqft"))
        ]

        avg_sale_price = avg(sale_prices)
        med_sale_price = median(sale_prices)
        avg_sale_psf = avg(sale_psf)
        med_sale_psf = median(sale_psf)
        avg_rent = avg(rent_amounts)
        med_rent = median(rent_amounts)
        avg_rent_psf = avg(rent_psf)
        med_rent_psf = median(rent_psf)

        roi = med_rent / med_sale_price * 100 if med_sale_price and med_rent else None
        liquidity_score = min(100, (len(s) + len(r)) / 50 * 100)
        yield_score = score_0_100(roi, 3, 10)
        price_score = 50

        if roi is not None:
            investment_score = (yield_score or 0) * 0.45 + liquidity_score * 0.35 + price_score * 0.20
        else:
            investment_score = liquidity_score * 0.5

        if roi is None:
            price_position = "INSUFFICIENT DATA"
        elif roi >= 8:
            price_position = "HIGH YIELD"
        elif roi >= 6:
            price_position = "HEALTHY YIELD"
        elif roi >= 4:
            price_position = "MODERATE YIELD"
        else:
            price_position = "LOW YIELD"

        recommendation, conclusion = economic_conclusion(
            area, building, property_type, segment,
            len(s), len(r), med_sale_price, med_sale_psf, med_rent,
            roi, liquidity_score, investment_score
        )

        rows.append({
            "calculated_at": now,
            "area_name": area,
            "building_name": building,
            "property_type": property_type,
            "unit_segment": segment,
            "sales_count": len(s),
            "rents_count": len(r),
            "avg_sale_price": avg_sale_price,
            "median_sale_price": med_sale_price,
            "avg_sale_psf": avg_sale_psf,
            "median_sale_psf": med_sale_psf,
            "avg_rent": avg_rent,
            "median_rent": med_rent,
            "avg_rent_psf": avg_rent_psf,
            "median_rent_psf": med_rent_psf,
            "gross_roi_percent": roi,
            "liquidity_score": liquidity_score,
            "yield_score": yield_score,
            "price_score": price_score,
            "investment_score": investment_score,
            "price_position": price_position,
            "recommendation": recommendation,
            "economic_conclusion": conclusion,
        })

    return rows


def calculate_area_summary(building_rows: List[dict]) -> List[dict]:
    grouped: Dict[Tuple[str, str, str], List[dict]] = {}

    for row in building_rows:
        key = (row["area_name"], row["property_type"], row["unit_segment"])
        grouped.setdefault(key, []).append(row)

    now = datetime.now(UTC).replace(tzinfo=None)
    out = []

    for (area, property_type, segment), rows in grouped.items():
        sales_count = sum(r["sales_count"] or 0 for r in rows)
        rents_count = sum(r["rents_count"] or 0 for r in rows)

        avg_sale_price = avg([safe_float(r["avg_sale_price"]) for r in rows])
        med_sale_price = median([safe_float(r["median_sale_price"]) for r in rows])
        avg_sale_psf = avg([safe_float(r["avg_sale_psf"]) for r in rows])
        med_sale_psf = median([safe_float(r["median_sale_psf"]) for r in rows])
        avg_rent_value = avg([safe_float(r["avg_rent"]) for r in rows])
        med_rent_value = median([safe_float(r["median_rent"]) for r in rows])
        roi = median([safe_float(r["gross_roi_percent"]) for r in rows])
        liquidity_score = min(100, (sales_count + rents_count) / 300 * 100)
        investment_score = median([safe_float(r["investment_score"]) for r in rows])

        if investment_score and investment_score >= 75:
            recommendation = "BUY"
        elif investment_score and investment_score >= 55:
            recommendation = "SELECTIVE BUY / HOLD"
        else:
            recommendation = "WATCH / NEGOTIATE"

        conclusion = (
            f"{area}. Сегмент: {property_type}, {segment}. "
            f"Районная сводка: {sales_count} продаж и {rents_count} арендных контрактов. "
            f"Медианная цена покупки: {med_sale_price or 0:,.0f} AED. "
            f"Медианная цена за sqft: {med_sale_psf or 0:,.0f} AED. "
            f"Медианная аренда: {med_rent_value or 0:,.0f} AED. "
            f"Ориентир gross ROI: {roi or 0:.2f}%. "
            f"Ликвидность района: {liquidity_score:.0f}/100. "
            f"Инвестиционный вывод: {recommendation}."
        )

        out.append({
            "calculated_at": now,
            "area_name": area,
            "property_type": property_type,
            "unit_segment": segment,
            "sales_count": sales_count,
            "rents_count": rents_count,
            "avg_sale_price": avg_sale_price,
            "median_sale_price": med_sale_price,
            "avg_sale_psf": avg_sale_psf,
            "median_sale_psf": med_sale_psf,
            "avg_rent": avg_rent_value,
            "median_rent": med_rent_value,
            "gross_roi_percent": roi,
            "liquidity_score": liquidity_score,
            "investment_score": investment_score,
            "recommendation": recommendation,
            "economic_conclusion": conclusion,
        })

    return out


def metrics_for_deals(sales: List[dict], rents: List[dict]) -> dict:
    sale_prices = [safe_float(x.get("amount")) for x in sales]
    sale_psf = [
        safe_float(x.get("amount")) / safe_float(x.get("size_sqft"))
        for x in sales
        if safe_float(x.get("amount")) and safe_float(x.get("size_sqft"))
    ]
    rent_amounts = [safe_float(x.get("amount")) for x in rents]

    med_sale = median(sale_prices)
    med_psf = median(sale_psf)
    med_rent = median(rent_amounts)
    roi = med_rent / med_sale * 100 if med_sale and med_rent else None

    return {
        "sales_count": len(sales),
        "rents_count": len(rents),
        "median_sale_price": med_sale,
        "median_sale_psf": med_psf,
        "median_rent": med_rent,
        "roi": roi,
        "volume": len(sales) + len(rents),
    }


def filter_period(deals: List[dict], start: datetime, end: datetime) -> List[dict]:
    out = []
    for d in deals:
        dt = d.get("deal_date")
        if not dt:
            continue
        if isinstance(dt, datetime):
            dd = dt.date()
        else:
            dd = dt
        if start.date() <= dd < end.date():
            out.append(d)
    return out


def trend_label(momentum: Optional[float]) -> str:
    if momentum is None:
        return "INSUFFICIENT DATA"
    if momentum >= 70:
        return "STRONG UPWARD MOMENTUM"
    if momentum >= 55:
        return "POSITIVE MOMENTUM"
    if momentum >= 45:
        return "STABLE"
    if momentum >= 30:
        return "WEAKENING"
    return "NEGATIVE MOMENTUM"


def period_conclusion(
    scope: str,
    name: str,
    period_code: str,
    property_type: str,
    segment: str,
    sale_change: Optional[float],
    psf_change: Optional[float],
    rent_change: Optional[float],
    roi_change: Optional[float],
    volume_change: Optional[float],
    momentum: Optional[float],
) -> str:
    label = trend_label(momentum)

    def fmt(v, suffix="%"):
        return "нет данных" if v is None else f"{v:+.2f}{suffix}"

    return (
        f"{scope}: {name}. Период: {period_code}. Сегмент: {property_type}, {segment}. "
        f"Динамика медианной цены: {fmt(sale_change)}. "
        f"Динамика цены за sqft: {fmt(psf_change)}. "
        f"Динамика аренды: {fmt(rent_change)}. "
        f"Изменение ROI: {fmt(roi_change, ' п.п.')}. "
        f"Изменение объёма сделок: {fmt(volume_change)}. "
        f"Momentum score: {momentum or 0:.0f}/100. "
        f"Рыночный вывод: {label}."
    )


def calculate_period_rows(
    sales: List[dict],
    rents: List[dict],
    group_mode: str,
) -> List[dict]:
    now_aware = datetime.now(UTC)
    calculated_at = now_aware.replace(tzinfo=None)
    rows = []

    for period_code, days in PERIOD_WINDOWS.items():
        current_start = now_aware - timedelta(days=days)
        previous_start = now_aware - timedelta(days=days * 2)
        current_end = now_aware
        previous_end = current_start

        current_sales_all = filter_period(sales, current_start, current_end)
        previous_sales_all = filter_period(sales, previous_start, previous_end)
        current_rents_all = filter_period(rents, current_start, current_end)
        previous_rents_all = filter_period(rents, previous_start, previous_end)

        grouped: Dict[Tuple, Dict[str, List[dict]]] = {}

        def key(d):
            if group_mode == "building":
                return key_for(d)
            if group_mode == "area":
                return area_key_for(d)
            if group_mode == "market":
                _area, _building, property_type, segment = key_for(d)
                return property_type, segment
            raise ValueError(group_mode)

        for d in current_sales_all:
            grouped.setdefault(key(d), {"cs": [], "ps": [], "cr": [], "pr": []})["cs"].append(d)
        for d in previous_sales_all:
            grouped.setdefault(key(d), {"cs": [], "ps": [], "cr": [], "pr": []})["ps"].append(d)
        for d in current_rents_all:
            grouped.setdefault(key(d), {"cs": [], "ps": [], "cr": [], "pr": []})["cr"].append(d)
        for d in previous_rents_all:
            grouped.setdefault(key(d), {"cs": [], "ps": [], "cr": [], "pr": []})["pr"].append(d)

        for k, data in grouped.items():
            current = metrics_for_deals(data["cs"], data["cr"])
            previous = metrics_for_deals(data["ps"], data["pr"])

            if current["volume"] < 3 and previous["volume"] < 3:
                continue

            sale_change = pct_change(current["median_sale_price"], previous["median_sale_price"])
            psf_change = pct_change(current["median_sale_psf"], previous["median_sale_psf"])
            rent_change = pct_change(current["median_rent"], previous["median_rent"])
            roi_change = None
            if current["roi"] is not None and previous["roi"] is not None:
                roi_change = current["roi"] - previous["roi"]
            volume_change = pct_change(current["volume"], previous["volume"])

            components = []
            if psf_change is not None:
                components.append(score_0_100(psf_change, -15, 15))
            if rent_change is not None:
                components.append(score_0_100(rent_change, -15, 15))
            if roi_change is not None:
                components.append(score_0_100(roi_change, -3, 3))
            if volume_change is not None:
                components.append(score_0_100(volume_change, -50, 50))

            momentum = avg(components) if components else None
            label = trend_label(momentum)

            if group_mode == "building":
                area_name, building_name, property_type, segment = k
                conclusion = period_conclusion(
                    "Здание", f"{building_name}, {area_name}", period_code,
                    property_type, segment, sale_change, psf_change,
                    rent_change, roi_change, volume_change, momentum
                )
                rows.append({
                    "group_mode": group_mode,
                    "calculated_at": calculated_at,
                    "period_code": period_code,
                    "current_days": days,
                    "previous_days": days,
                    "area_name": area_name,
                    "building_name": building_name,
                    "property_type": property_type,
                    "unit_segment": segment,
                    "current": current,
                    "previous": previous,
                    "sale_change": sale_change,
                    "psf_change": psf_change,
                    "rent_change": rent_change,
                    "roi_change": roi_change,
                    "volume_change": volume_change,
                    "momentum": momentum,
                    "trend_label": label,
                    "conclusion": conclusion,
                })
            elif group_mode == "area":
                area_name, property_type, segment = k
                conclusion = period_conclusion(
                    "Район", area_name, period_code,
                    property_type, segment, sale_change, psf_change,
                    rent_change, roi_change, volume_change, momentum
                )
                rows.append({
                    "group_mode": group_mode,
                    "calculated_at": calculated_at,
                    "period_code": period_code,
                    "current_days": days,
                    "previous_days": days,
                    "area_name": area_name,
                    "property_type": property_type,
                    "unit_segment": segment,
                    "current": current,
                    "previous": previous,
                    "sale_change": sale_change,
                    "psf_change": psf_change,
                    "rent_change": rent_change,
                    "roi_change": roi_change,
                    "volume_change": volume_change,
                    "momentum": momentum,
                    "trend_label": label,
                    "conclusion": conclusion,
                })
            else:
                property_type, segment = k
                temperature = label
                conclusion = period_conclusion(
                    "Рынок Dubai", "All areas", period_code,
                    property_type, segment, sale_change, psf_change,
                    rent_change, roi_change, volume_change, momentum
                )
                rows.append({
                    "group_mode": group_mode,
                    "calculated_at": calculated_at,
                    "period_code": period_code,
                    "current_days": days,
                    "previous_days": days,
                    "property_type": property_type,
                    "unit_segment": segment,
                    "current": current,
                    "previous": previous,
                    "sale_change": sale_change,
                    "psf_change": psf_change,
                    "rent_change": rent_change,
                    "roi_change": roi_change,
                    "volume_change": volume_change,
                    "market_temperature": temperature,
                    "conclusion": conclusion,
                })

    return rows


def save_building_rows(conn, rows: List[dict]) -> None:
    with conn.cursor() as cur:
        if rows:
            execute_values(
                cur,
                """
                INSERT INTO building_roi_summary (
                    calculated_at, area_name, building_name, property_type, unit_segment,
                    sales_count, rents_count,
                    avg_sale_price, median_sale_price, avg_sale_psf, median_sale_psf,
                    avg_rent, median_rent, avg_rent_psf, median_rent_psf,
                    gross_roi_percent, liquidity_score, yield_score, price_score, investment_score,
                    price_position, recommendation, economic_conclusion
                ) VALUES %s
                """,
                [
                    (
                        r["calculated_at"], r["area_name"], r["building_name"], r["property_type"], r["unit_segment"],
                        r["sales_count"], r["rents_count"], r["avg_sale_price"], r["median_sale_price"],
                        r["avg_sale_psf"], r["median_sale_psf"], r["avg_rent"], r["median_rent"],
                        r["avg_rent_psf"], r["median_rent_psf"], r["gross_roi_percent"], r["liquidity_score"],
                        r["yield_score"], r["price_score"], r["investment_score"], r["price_position"],
                        r["recommendation"], r["economic_conclusion"],
                    )
                    for r in rows
                ],
                page_size=1000,
            )
    conn.commit()


def save_area_rows(conn, rows: List[dict]) -> None:
    with conn.cursor() as cur:
        if rows:
            execute_values(
                cur,
                """
                INSERT INTO area_roi_summary (
                    calculated_at, area_name, property_type, unit_segment,
                    sales_count, rents_count,
                    avg_sale_price, median_sale_price, avg_sale_psf, median_sale_psf,
                    avg_rent, median_rent, gross_roi_percent,
                    liquidity_score, investment_score, recommendation, economic_conclusion
                ) VALUES %s
                """,
                [
                    (
                        r["calculated_at"], r["area_name"], r["property_type"], r["unit_segment"],
                        r["sales_count"], r["rents_count"], r["avg_sale_price"], r["median_sale_price"],
                        r["avg_sale_psf"], r["median_sale_psf"], r["avg_rent"], r["median_rent"],
                        r["gross_roi_percent"], r["liquidity_score"], r["investment_score"],
                        r["recommendation"], r["economic_conclusion"],
                    )
                    for r in rows
                ],
                page_size=1000,
            )
    conn.commit()


def save_building_period_rows(conn, rows: List[dict]) -> None:
    rows = [r for r in rows if r["group_mode"] == "building"]
    with conn.cursor() as cur:
        if rows:
            execute_values(
                cur,
                """
                INSERT INTO building_period_comparison (
                    calculated_at, period_code, current_days, previous_days,
                    area_name, building_name, property_type, unit_segment,
                    current_sales_count, previous_sales_count, current_rents_count, previous_rents_count,
                    current_median_sale_price, previous_median_sale_price, sale_price_change_percent,
                    current_median_sale_psf, previous_median_sale_psf, sale_psf_change_percent,
                    current_median_rent, previous_median_rent, rent_change_percent,
                    current_roi_percent, previous_roi_percent, roi_change_pp,
                    volume_change_percent, momentum_score, trend_label, professional_conclusion
                ) VALUES %s
                """,
                [
                    (
                        r["calculated_at"], r["period_code"], r["current_days"], r["previous_days"],
                        r["area_name"], r["building_name"], r["property_type"], r["unit_segment"],
                        r["current"]["sales_count"], r["previous"]["sales_count"], r["current"]["rents_count"], r["previous"]["rents_count"],
                        r["current"]["median_sale_price"], r["previous"]["median_sale_price"], r["sale_change"],
                        r["current"]["median_sale_psf"], r["previous"]["median_sale_psf"], r["psf_change"],
                        r["current"]["median_rent"], r["previous"]["median_rent"], r["rent_change"],
                        r["current"]["roi"], r["previous"]["roi"], r["roi_change"],
                        r["volume_change"], r["momentum"], r["trend_label"], r["conclusion"],
                    )
                    for r in rows
                ],
                page_size=1000,
            )
    conn.commit()


def save_area_period_rows(conn, rows: List[dict]) -> None:
    rows = [r for r in rows if r["group_mode"] == "area"]
    with conn.cursor() as cur:
        if rows:
            execute_values(
                cur,
                """
                INSERT INTO area_period_comparison (
                    calculated_at, period_code, current_days, previous_days,
                    area_name, property_type, unit_segment,
                    current_sales_count, previous_sales_count, current_rents_count, previous_rents_count,
                    current_median_sale_price, previous_median_sale_price, sale_price_change_percent,
                    current_median_sale_psf, previous_median_sale_psf, sale_psf_change_percent,
                    current_median_rent, previous_median_rent, rent_change_percent,
                    current_roi_percent, previous_roi_percent, roi_change_pp,
                    volume_change_percent, momentum_score, trend_label, professional_conclusion
                ) VALUES %s
                """,
                [
                    (
                        r["calculated_at"], r["period_code"], r["current_days"], r["previous_days"],
                        r["area_name"], r["property_type"], r["unit_segment"],
                        r["current"]["sales_count"], r["previous"]["sales_count"], r["current"]["rents_count"], r["previous"]["rents_count"],
                        r["current"]["median_sale_price"], r["previous"]["median_sale_price"], r["sale_change"],
                        r["current"]["median_sale_psf"], r["previous"]["median_sale_psf"], r["psf_change"],
                        r["current"]["median_rent"], r["previous"]["median_rent"], r["rent_change"],
                        r["current"]["roi"], r["previous"]["roi"], r["roi_change"],
                        r["volume_change"], r["momentum"], r["trend_label"], r["conclusion"],
                    )
                    for r in rows
                ],
                page_size=1000,
            )
    conn.commit()


def save_market_period_rows(conn, rows: List[dict]) -> None:
    rows = [r for r in rows if r["group_mode"] == "market"]
    with conn.cursor() as cur:
        if rows:
            execute_values(
                cur,
                """
                INSERT INTO market_period_summary (
                    calculated_at, period_code, current_days, previous_days,
                    property_type, unit_segment,
                    current_sales_count, previous_sales_count, current_rents_count, previous_rents_count,
                    current_median_sale_price, previous_median_sale_price, sale_price_change_percent,
                    current_median_sale_psf, previous_median_sale_psf, sale_psf_change_percent,
                    current_median_rent, previous_median_rent, rent_change_percent,
                    current_roi_percent, previous_roi_percent, roi_change_pp,
                    volume_change_percent, market_temperature, professional_conclusion
                ) VALUES %s
                """,
                [
                    (
                        r["calculated_at"], r["period_code"], r["current_days"], r["previous_days"],
                        r["property_type"], r["unit_segment"],
                        r["current"]["sales_count"], r["previous"]["sales_count"], r["current"]["rents_count"], r["previous"]["rents_count"],
                        r["current"]["median_sale_price"], r["previous"]["median_sale_price"], r["sale_change"],
                        r["current"]["median_sale_psf"], r["previous"]["median_sale_psf"], r["psf_change"],
                        r["current"]["median_rent"], r["previous"]["median_rent"], r["rent_change"],
                        r["current"]["roi"], r["previous"]["roi"], r["roi_change"],
                        r["volume_change"], r["market_temperature"], r["conclusion"],
                    )
                    for r in rows
                ],
                page_size=1000,
            )
    conn.commit()


def save_recommendations(conn, rows: List[dict]) -> None:
    top = sorted(
        [r for r in rows if r.get("investment_score") is not None],
        key=lambda x: x["investment_score"],
        reverse=True,
    )[:500]

    now = datetime.now(UTC).replace(tzinfo=None)

    with conn.cursor() as cur:
        if top:
            execute_values(
                cur,
                """
                INSERT INTO investment_recommendations (
                    calculated_at, area_name, building_name, property_type, unit_segment,
                    investment_score, gross_roi_percent, liquidity_score,
                    avg_sale_price, median_rent, recommendation, reason
                ) VALUES %s
                """,
                [
                    (
                        now, r["area_name"], r["building_name"], r["property_type"], r["unit_segment"],
                        r["investment_score"], r["gross_roi_percent"], r["liquidity_score"],
                        r["avg_sale_price"], r["median_rent"], r["recommendation"], r["economic_conclusion"],
                    )
                    for r in top
                ],
                page_size=1000,
            )
    conn.commit()


def count_table(conn, schema: str, table: str) -> int:
    if not has_table(conn, schema, table):
        return 0
    with conn.cursor() as cur:
        cur.execute(f'SELECT COUNT(*) AS c FROM "{schema}"."{table}"')
        return int(cur.fetchone()["c"])


def run_cycle(live_conn, archive_conn, intel_conn) -> None:
    logging.info("Creating intelligence tables...")
    create_tables(intel_conn)

    logging.info("Loading sales from archive...")
    archive_sales = fetch_normalized_deals(archive_conn, [SALE_TABLES[0]], "sale")

    logging.info("Loading sales from live...")
    live_sales = fetch_normalized_deals(live_conn, [SALE_TABLES[1]], "sale")

    logging.info("Loading rents from archive...")
    archive_rents = fetch_normalized_deals(archive_conn, [RENT_TABLES[0]], "rent")

    logging.info("Loading rents from live...")
    live_rents = fetch_normalized_deals(live_conn, [RENT_TABLES[1]], "rent")

    sales = archive_sales + live_sales
    rents = archive_rents + live_rents

    logging.info("Sales loaded: %s", len(sales))
    logging.info("Rents loaded: %s", len(rents))

    logging.info("Calculating building ROI summary by area/building/property_type/rooms...")
    building_rows = calculate_building_summary(sales, rents)
    logging.info("Building summaries calculated: %s", len(building_rows))

    logging.info("Calculating area ROI summary by area/property_type/rooms...")
    area_rows = calculate_area_summary(building_rows)
    logging.info("Area summaries calculated: %s", len(area_rows))

    logging.info("Calculating building period comparisons...")
    building_period_rows = calculate_period_rows(sales, rents, "building")
    logging.info("Building period rows calculated: %s", len(building_period_rows))

    logging.info("Calculating area period comparisons...")
    area_period_rows = calculate_period_rows(sales, rents, "area")
    logging.info("Area period rows calculated: %s", len(area_period_rows))

    logging.info("Calculating market period summaries...")
    market_period_rows = calculate_period_rows(sales, rents, "market")
    logging.info("Market period rows calculated: %s", len(market_period_rows))

    logging.info("Saving building summaries...")
    save_building_rows(intel_conn, building_rows)

    logging.info("Saving area summaries...")
    save_area_rows(intel_conn, area_rows)

    logging.info("Saving building period comparisons...")
    save_building_period_rows(intel_conn, building_period_rows)

    logging.info("Saving area period comparisons...")
    save_area_period_rows(intel_conn, area_period_rows)

    logging.info("Saving market period summaries...")
    save_market_period_rows(intel_conn, market_period_rows)

    logging.info("Saving investment recommendations...")
    save_recommendations(intel_conn, building_rows)

    live_sales_count = count_table(live_conn, "public", "dld_transactions_full")
    live_rents_count = count_table(live_conn, "public", "dld_rents_full")
    archive_sales_count = count_table(archive_conn, "public", "dld_sale_archive")
    archive_rents_count = count_table(archive_conn, "public", "dld_rent_archive")

    with intel_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO intelligence_status (
                created_at,
                live_sales_count,
                live_rents_count,
                archive_sales_count,
                archive_rents_count,
                building_summaries_count,
                area_summaries_count,
                building_period_rows_count,
                area_period_rows_count,
                market_period_rows_count,
                status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                datetime.now(UTC).replace(tzinfo=None),
                live_sales_count,
                live_rents_count,
                archive_sales_count,
                archive_rents_count,
                len(building_rows),
                len(area_rows),
                len(building_period_rows),
                len(area_period_rows),
                len(market_period_rows),
                "ok",
            ),
        )

    intel_conn.commit()
    logging.info("Intelligence status updated successfully")
    logging.info("Cycle completed successfully")


def main() -> None:
    require_env()

    logging.info("=" * 80)
    logging.info("Dubai DLD Professional Intelligence Engine v3 started")
    logging.info("Mode: archive + live + intelligence")
    logging.info("Static analytics: ROI / yield / liquidity / recommendations")
    logging.info("Temporal analytics: 30d / 90d / 180d / 365d period comparisons")
    logging.info("Grouping: area + building + property type + rooms/unit segment")
    logging.info("Note: current DLD tables do not expose exact unit_number; transaction_id/contract_id are source identifiers only.")
    logging.info("=" * 80)

    live_conn = connect(LIVE_DATABASE_URL)
    archive_conn = connect(ARCHIVE_DATABASE_URL)
    intel_conn = connect(INTELLIGENCE_DATABASE_URL)

    logging.info("LIVE DB connected")
    logging.info("ARCHIVE DB connected")
    logging.info("INTELLIGENCE DB connected")

    while True:
        try:
            run_cycle(live_conn, archive_conn, intel_conn)
        except Exception as exc:
            logging.exception("Intelligence cycle failed: %s", exc)

            for conn in (live_conn, archive_conn, intel_conn):
                try:
                    conn.rollback()
                except Exception:
                    pass

        logging.info("Sleeping %s seconds before next intelligence cycle...", CYCLE_SECONDS)
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    main()
