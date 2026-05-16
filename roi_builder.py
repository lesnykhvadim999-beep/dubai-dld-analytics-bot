import os
import psycopg2

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

print("CONNECTED", flush=True)


def table_columns(table):
    cur.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
    """, (table,))
    return {r[0] for r in cur.fetchall()}


def first_working_numeric_column(table, candidates):
    cols = table_columns(table)

    for col in candidates:
        if col not in cols:
            continue

        try:
            cur.execute(f"""
                SELECT COUNT(*)
                FROM {table}
                WHERE {col} IS NOT NULL
                  AND {col} > 0
            """)
            count = cur.fetchone()[0]

            print(f"{table}.{col}: {count} positive rows", flush=True)

            if count > 0:
                return col
        except Exception as e:
            conn.rollback()
            print(f"SKIP {table}.{col}: {e}", flush=True)

    raise RuntimeError(f"No usable numeric column found for {table}")


sales_cols = table_columns("dld_transactions_full")
rent_cols = table_columns("dld_rents_full")

if "area_en" not in sales_cols:
    raise RuntimeError("dld_transactions_full.area_en not found")

if "area_en" not in rent_cols:
    raise RuntimeError("dld_rents_full.area_en not found")

sale_price_col = first_working_numeric_column(
    "dld_transactions_full",
    [
        "actual_worth",
        "actualWorth",
        "amount",
        "transaction_amount",
        "procedure_value",
        "value",
        "price",
        "sale_price",
        "meter_sale_price",
    ],
)

sale_area_col = first_working_numeric_column(
    "dld_transactions_full",
    [
        "procedure_area",
        "actual_area",
        "actualArea",
        "area",
        "size",
    ],
)

rent_price_col = first_working_numeric_column(
    "dld_rents_full",
    [
        "annual_amount",
        "contract_amount",
        "contract_value",
        "rent_amount",
        "amount",
        "rent_value",
    ],
)

print(f"USING SALE PRICE COLUMN: {sale_price_col}", flush=True)
print(f"USING SALE AREA COLUMN: {sale_area_col}", flush=True)
print(f"USING RENT COLUMN: {rent_price_col}", flush=True)

cur.execute(f"""
DROP TABLE IF EXISTS roi_analytics;

CREATE TABLE roi_analytics AS

WITH sales AS (
    SELECT
        LOWER(TRIM(area_en)) AS area_key,
        MIN(area_en) AS sales_area,

        COUNT(*) AS sales_deals,

        AVG({sale_price_col}) AS avg_property_price,
        AVG({sale_area_col}) AS avg_area

    FROM dld_transactions_full

    WHERE area_en IS NOT NULL
      AND {sale_price_col} IS NOT NULL
      AND {sale_price_col} > 0
      AND {sale_area_col} IS NOT NULL
      AND {sale_area_col} > 0

    GROUP BY LOWER(TRIM(area_en))
),

rents AS (
    SELECT
        LOWER(TRIM(area_en)) AS area_key,
        MIN(area_en) AS rent_area,

        COUNT(*) AS rent_deals,

        AVG({rent_price_col}) AS avg_annual_rent

    FROM dld_rents_full

    WHERE area_en IS NOT NULL
      AND {rent_price_col} IS NOT NULL
      AND {rent_price_col} > 0

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

    ROUND(
        ((r.avg_annual_rent / NULLIF(s.avg_property_price, 0)) * 100)::numeric,
        2
    ) AS gross_yield_percent,

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
