import os
import time
import math
import logging
from datetime import datetime, UTC
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

CYCLE_SECONDS = int(os.getenv("INTELLIGENCE_CYCLE_SECONDS", "21600"))  # 6 hours


SALE_TABLES = [
    ("archive", "public", "dld_sale_archive"),
    ("live", "public", "dld_transactions_full"),
]

RENT_TABLES = [
    ("archive", "public", "dld_rent_archive"),
    ("live", "public", "dld_rents_full"),
]


COLUMN_CANDIDATES = {
    "date": ["transaction_date", "instance_date", "procedure_date", "date", "registration_date"],
    "area": ["area_name_en", "area_name", "area", "master_project_en", "master_project"],
    "building": ["building_name_en", "building_name", "project_name_en", "project_name", "building", "project"],
    "property_type": ["property_type_en", "property_type", "property_sub_type_en", "property_sub_type", "property_usage_en"],
    "unit_type": ["property_sub_type_en", "property_sub_type", "unit_type", "unit_type_en"],
    "rooms": ["rooms_en", "rooms", "rooms_ar", "bedrooms", "bedroom"],
    "unit": ["unit_number", "unit_no", "unit", "property_number", "property_id"],
    "size_sqm": ["actual_area", "area_sqm", "property_size_sqm", "size_sqm", "procedure_area"],
    "size_sqft": ["area_sqft", "property_size_sqft", "size_sqft", "built_up_area_sqft"],
    "price": ["actual_worth", "amount", "price", "sale_price", "value", "transaction_amount"],
    "rent": ["annual_amount", "rent_value", "rent_amount", "amount", "contract_amount", "price"],
    "procedure": ["procedure_name_en", "procedure_name", "procedure", "transaction_type_en", "transaction_type"],
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


def get_columns(conn, schema: str, table: str) -> List[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table),
        )
        return [r["column_name"] for r in cur.fetchall()]


def has_table(conn, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = %s AND table_name = %s
            ) AS exists
            """,
            (schema, table),
        )
        return bool(cur.fetchone()["exists"])


def pick_col(columns: List[str], logical: str) -> Optional[str]:
    lower_map = {c.lower(): c for c in columns}
    for candidate in COLUMN_CANDIDATES[logical]:
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
    return f'NULLIF(REGEXP_REPLACE("{col}"::text, \'[^0-9.]\\\', \'\', \'g\'), \'\')::numeric'


def date_expr(columns: List[str]) -> str:
    col = pick_col(columns, "date")
    if not col:
        return "NULL::date"
    return f'("{col}"::text)::date'


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

    size_sqm = numeric_expr(columns, "size_sqm")
    size_sqft = numeric_expr(columns, "size_sqft")

    size_sqft_final = f"""
        CASE
            WHEN {size_sqft} IS NOT NULL AND {size_sqft} > 0 THEN {size_sqft}
            WHEN {size_sqm} IS NOT NULL AND {size_sqm} > 0 THEN {size_sqm} * 10.7639
            ELSE NULL
        END
    """

    amount = numeric_expr(columns, amount_logical)

    return f"""
        SELECT
            '{source_name}'::text AS source_db,
            '{deal_type}'::text AS deal_type,
            {date_expr(columns)} AS deal_date,
            NULLIF(TRIM(COALESCE({sql_expr(columns, "area", "NULL")}::text, '')), '') AS area_name,
            NULLIF(TRIM(COALESCE({sql_expr(columns, "building", "NULL")}::text, '')), '') AS building_name,
            NULLIF(TRIM(COALESCE({sql_expr(columns, "property_type", "NULL")}::text, '')), '') AS property_type,
            NULLIF(TRIM(COALESCE({sql_expr(columns, "unit_type", "NULL")}::text, '')), '') AS unit_type,
            NULLIF(TRIM(COALESCE({sql_expr(columns, "rooms", "NULL")}::text, '')), '') AS rooms,
            NULLIF(TRIM(COALESCE({sql_expr(columns, "unit", "NULL")}::text, '')), '') AS unit_number,
            ({size_sqft_final})::numeric AS size_sqft,
            ({amount})::numeric AS amount,
            NULLIF(TRIM(COALESCE({sql_expr(columns, "procedure", "NULL")}::text, '')), '') AS procedure_name
        FROM "{schema}"."{table}"
        WHERE ({amount}) IS NOT NULL
          AND ({amount}) > 0
    """


def fetch_normalized_deals(conn, tables: List[Tuple[str, str, str]], deal_type: str) -> List[dict]:
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


def median(values: List[float]) -> Optional[float]:
    values = sorted([v for v in values if v is not None and v > 0])
    if not values:
        return None
    n = len(values)
    mid = n // 2
    if n % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def avg(values: List[float]) -> Optional[float]:
    values = [v for v in values if v is not None and v > 0]
    if not values:
        return None
    return sum(values) / len(values)


def percentile(values: List[float], p: float) -> Optional[float]:
    values = sorted([v for v in values if v is not None and v > 0])
    if not values:
        return None
    k = (len(values) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return values[int(k)]
    return values[f] * (c - k) + values[c] * (k - f)


def score_0_100(value: Optional[float], low: float, high: float) -> Optional[float]:
    if value is None:
        return None
    if high == low:
        return 50
    return max(0, min(100, (value - low) / (high - low) * 100))


def normalize_text(v: Optional[str]) -> str:
    if not v:
        return "Unknown"
    return " ".join(str(v).strip().split())


def key_for(deal: dict) -> Tuple[str, str, str, str]:
    return (
        normalize_text(deal.get("area_name")),
        normalize_text(deal.get("building_name")),
        normalize_text(deal.get("property_type")),
        normalize_text(deal.get("rooms") or deal.get("unit_type")),
    )


def create_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS building_roi_summary (
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
            CREATE TABLE IF NOT EXISTS area_roi_summary (
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
            CREATE TABLE IF NOT EXISTS investment_recommendations (
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
            CREATE TABLE IF NOT EXISTS intelligence_status (
                id BIGSERIAL PRIMARY KEY,
                created_at TIMESTAMP NOT NULL,
                live_sales_count BIGINT,
                live_rents_count BIGINT,
                archive_sales_count BIGINT,
                archive_rents_count BIGINT,
                building_summaries_count BIGINT,
                area_summaries_count BIGINT,
                status TEXT
            )
        """)

        cur.execute("CREATE INDEX IF NOT EXISTS idx_building_roi_lookup ON building_roi_summary (area_name, building_name, property_type, unit_segment)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_area_roi_lookup ON area_roi_summary (area_name, property_type, unit_segment)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_recommendations_score ON investment_recommendations (investment_score DESC)")

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
    if roi is None:
        recommendation = "WATCH"
        text = (
            f"{building}, {area}: недостаточно стабильных данных по аренде или продаже для точного ROI. "
            f"Рекомендуется использовать как предварительный ориентир и проверять сопоставимые сделки вручную."
        )
        return recommendation, text

    if investment_score is not None and investment_score >= 75:
        recommendation = "BUY"
    elif investment_score is not None and investment_score >= 55:
        recommendation = "HOLD / SELECTIVE BUY"
    else:
        recommendation = "AVOID / NEGOTIATE HARD"

    text = (
        f"{building}, {area}. Сегмент: {property_type}, {segment}. "
        f"Медианная цена покупки: {median_sale_price:,.0f} AED, "
        f"медианная цена за sqft: {median_sale_psf:,.0f} AED, "
        f"медианная годовая аренда: {median_rent:,.0f} AED. "
        f"Ориентировочный gross ROI: {roi:.2f}%. "
        f"Ликвидность: {liquidity_score:.0f}/100, инвестиционный скоринг: {investment_score:.0f}/100. "
        f"Вывод: {recommendation}. "
        f"Основано на {sales_count} сделках продаж и {rents_count} сделках аренды."
    )
    return recommendation, text


def calculate_building_summary(sales: List[dict], rents: List[dict]) -> List[dict]:
    grouped: Dict[Tuple[str, str, str, str], Dict[str, List[dict]]] = {}

    for d in sales:
        grouped.setdefault(key_for(d), {"sales": [], "rents": []})["sales"].append(d)

    for d in rents:
        grouped.setdefault(key_for(d), {"sales": [], "rents": []})["rents"].append(d)

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

        rents_amount = [safe_float(x.get("amount")) for x in r]
        rent_psf = [
            safe_float(x.get("amount")) / safe_float(x.get("size_sqft"))
            for x in r
            if safe_float(x.get("amount")) and safe_float(x.get("size_sqft"))
        ]

        avg_sale_price = avg(sale_prices)
        med_sale_price = median(sale_prices)
        avg_sale_psf = avg(sale_psf)
        med_sale_psf = median(sale_psf)

        avg_rent = avg(rents_amount)
        med_rent = median(rents_amount)
        avg_rent_psf = avg(rent_psf)
        med_rent_psf = median(rent_psf)

        roi = None
        if med_sale_price and med_rent:
            roi = med_rent / med_sale_price * 100

        liquidity_score = min(100, (len(s) + len(r)) / 50 * 100)
        yield_score = score_0_100(roi, 3, 10)
        price_score = 50

        if roi is not None:
            investment_score = (
                (yield_score or 0) * 0.45
                + liquidity_score * 0.35
                + price_score * 0.20
            )
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
            area,
            building,
            property_type,
            segment,
            len(s),
            len(r),
            med_sale_price or 0,
            med_sale_psf or 0,
            med_rent or 0,
            roi,
            liquidity_score,
            investment_score,
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
        key = (
            row["area_name"],
            row["property_type"],
            row["unit_segment"],
        )
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
            recommendation = "HOLD / SELECTIVE BUY"
        else:
            recommendation = "WATCH / NEGOTIATE"

        conclusion = (
            f"{area}. Сегмент: {property_type}, {segment}. "
            f"Сводка по району: {sales_count} продаж и {rents_count} арендных сделок. "
            f"Медианная цена покупки: {med_sale_price or 0:,.0f} AED, "
            f"медианная цена за sqft: {med_sale_psf or 0:,.0f} AED, "
            f"медианная аренда: {med_rent_value or 0:,.0f} AED, "
            f"ориентир gross ROI: {roi or 0:.2f}%. "
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


def save_building_rows(conn, rows: List[dict]) -> None:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE building_roi_summary")

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
                        r["sales_count"], r["rents_count"],
                        r["avg_sale_price"], r["median_sale_price"], r["avg_sale_psf"], r["median_sale_psf"],
                        r["avg_rent"], r["median_rent"], r["avg_rent_psf"], r["median_rent_psf"],
                        r["gross_roi_percent"], r["liquidity_score"], r["yield_score"], r["price_score"], r["investment_score"],
                        r["price_position"], r["recommendation"], r["economic_conclusion"],
                    )
                    for r in rows
                ],
                page_size=1000,
            )

    conn.commit()


def save_area_rows(conn, rows: List[dict]) -> None:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE area_roi_summary")

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
                        r["sales_count"], r["rents_count"],
                        r["avg_sale_price"], r["median_sale_price"], r["avg_sale_psf"], r["median_sale_psf"],
                        r["avg_rent"], r["median_rent"], r["gross_roi_percent"],
                        r["liquidity_score"], r["investment_score"], r["recommendation"], r["economic_conclusion"],
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
        cur.execute("TRUNCATE TABLE investment_recommendations")

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
                        now,
                        r["area_name"],
                        r["building_name"],
                        r["property_type"],
                        r["unit_segment"],
                        r["investment_score"],
                        r["gross_roi_percent"],
                        r["liquidity_score"],
                        r["avg_sale_price"],
                        r["median_rent"],
                        r["recommendation"],
                        r["economic_conclusion"],
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

    logging.info("Calculating building ROI summary...")
    building_rows = calculate_building_summary(sales, rents)
    logging.info("Building summaries calculated: %s", len(building_rows))

    logging.info("Calculating area ROI summary...")
    area_rows = calculate_area_summary(building_rows)
    logging.info("Area summaries calculated: %s", len(area_rows))

    logging.info("Saving building summaries...")
    save_building_rows(intel_conn, building_rows)

    logging.info("Saving area summaries...")
    save_area_rows(intel_conn, area_rows)

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
                status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                datetime.now(UTC).replace(tzinfo=None),
                live_sales_count,
                live_rents_count,
                archive_sales_count,
                archive_rents_count,
                len(building_rows),
                len(area_rows),
                "ok",
            ),
        )

    intel_conn.commit()
    logging.info("Cycle completed successfully")


def main() -> None:
    require_env()

    logging.info("=" * 80)
    logging.info("Dubai DLD Professional Intelligence Engine started")
    logging.info("Mode: archive + live + intelligence")
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
            try:
                live_conn.rollback()
            except Exception:
                pass
            try:
                archive_conn.rollback()
            except Exception:
                pass
            try:
                intel_conn.rollback()
            except Exception:
                pass

        logging.info("Sleeping %s seconds before next intelligence cycle...", CYCLE_SECONDS)
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    main()
