import os
import re
import psycopg2

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

print("CONNECTED", flush=True)


def get_columns(table):
    cur.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        ORDER BY ordinal_position
    """, (table,))
    return [r[0] for r in cur.fetchall()]


def q(col):
    return '"' + col.replace('"', '""') + '"'


def num_expr(col):
    return f"""
        NULLIF(
            regexp_replace({q(col)}::text, '[^0-9.]', '', 'g'),
            ''
        )::numeric
    """


def positive_count(table, col):
    try:
        cur.execute(f"""
            SELECT COUNT(*)
            FROM {table}
            WHERE {num_expr(col)} > 0
        """)
        return cur.fetchone()[0]
    except Exception:
        conn.rollback()
        return 0


def avg_value(table, col):
    try:
        cur.execute(f"""
            SELECT AVG({num_expr(col)})
            FROM {table}
            WHERE {num_expr(col)} > 0
        """)
        v = cur.fetchone()[0]
        return float(v) if v is not None else 0
    except Exception:
        conn.rollback()
        return 0


def pick_column(table, keywords, exclude_keywords=()):
    cols = get_columns(table)
    best = None

    for col in cols:
        low = col.lower()

        if any(x in low for x in exclude_keywords):
            continue

        keyword_score = 0
        for i, kw in enumerate(keywords):
            if kw in low:
                keyword_score = 100 - i
                break

        cnt = positive_count(table, col)
        if cnt <= 0:
            continue

        avg = avg_value(table, col)

        score = keyword_score * 1_000_000 + cnt + min(avg, 100_000_000)

        print(f"CANDIDATE {table}.{col}: count={cnt}, avg={avg}, score={score}", flush=True)

        if best is None or score > best["score"]:
            best = {
                "col": col,
                "count": cnt,
                "avg": avg,
                "score": score,
            }

    if not best:
        raise RuntimeError(f"No usable column found for {table}")

    print(f"SELECTED {table}: {best}", flush=True)
    return best["col"]


sales_price_col = pick_column(
    "dld_transactions_full",
    keywords=[
        "actual_worth",
        "worth",
        "amount",
        "value",
        "price",
        "sale",
        "procedure",
    ],
    exclude_keywords=[
        "id",
        "number",
        "date",
        "area_id",
        "area_en",
        "area_ar",
        "parking",
        "metro",
        "mall",
        "landmark",
        "free",
        "offplan",
    ],
)

sales_area_col = pick_column(
    "dld_transactions_full",
    keywords=[
        "procedure_area",
        "actual_area",
        "area",
        "size",
    ],
    exclude_keywords=[
        "id",
        "area_en",
        "area_ar",
        "price",
        "worth",
        "amount",
        "value",
        "date",
        "number",
    ],
)

rent_col = pick_column(
    "dld_rents_full",
    keywords=[
        "annual_amount",
        "contract_amount",
        "amount",
        "rent",
        "value",
        "price",
    ],
    exclude_keywords=[
        "id",
        "number",
        "date",
        "area_id",
        "area_en",
        "area_ar",
        "parking",
        "metro",
        "mall",
        "landmark",
    ],
)

print(f"USING SALES PRICE: {sales_price_col}", flush=True)
print(f"USING SALES AREA: {sales_area_col}", flush=True)
print(f"USING RENT: {rent_col}", flush=True)

cur.execute(f"""
DROP TABLE IF EXISTS roi_analytics;

CREATE TABLE roi_analytics AS

WITH sales AS (
    SELECT
        LOWER(TRIM(area_en)) AS area_key,
        MIN(area_en) AS sales_area,
        COUNT(*) AS sales_deals,
        AVG({num_expr(sales_price_col)}) AS avg_property_price,
        AVG({num_expr(sales_area_col)}) AS avg_area
    FROM dld_transactions_full
    WHERE area_en IS NOT NULL
      AND {num_expr(sales_price_col)} > 0
      AND {num_expr(sales_area_col)} > 0
    GROUP BY LOWER(TRIM(area_en))
),

rents AS (
    SELECT
        LOWER(TRIM(area_en)) AS area_key,
        MIN(area_en) AS rent_area,
        COUNT(*) AS rent_deals,
        AVG({num_expr(rent_col)}) AS avg_annual_rent
    FROM dld_rents_full
    WHERE area_en IS NOT NULL
      AND {num_expr(rent_col)} > 0
    GROUP BY LOWER(TRIM(area_en))
)

SELECT
    s.sales_area,
    r.rent_area,
    s.sales_deals,
    r.rent_deals,
    ROUND(s.avg_property_price::numeric, 2) AS avg_property_price,
    ROUND(s.avg_area::numeric, 2) AS avg_area,
    ROUND(r.avg_annual_rent::numeric, 2) AS avg_annual_rent,
    ROUND(((r.avg_annual_rent / NULLIF(s.avg_property_price, 0)) * 100)::numeric, 2) AS gross_yield_percent,
    NOW() AS updated_at
FROM sales s
JOIN rents r
ON s.area_key = r.area_key
WHERE s.avg_property_price > 0
  AND r.avg_annual_rent > 0;
""")

conn.commit()

cur.execute("SELECT COUNT(*) FROM roi_analytics;")
count = cur.fetchone()[0]

print(f"ROI TABLE CREATED: {count} ROWS", flush=True)

cur.close()
conn.close()

print("DONE", flush=True)
