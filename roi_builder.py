import os
import psycopg2

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

print("CONNECTED", flush=True)

cur.execute("""
DROP TABLE IF EXISTS roi_analytics;

CREATE TABLE roi_analytics AS

WITH sales AS (
    SELECT
        area_en,
        prop_type_en,

        COUNT(*) AS sales_deals,

        AVG(NULLIF(actual_worth, 0)) AS avg_sale_price,
        AVG(NULLIF(meter_sale_price, 0)) AS avg_meter_sale_price,
        AVG(NULLIF(actual_area, 0)) AS avg_area

    FROM dld_transactions_full

    WHERE actual_worth IS NOT NULL
      AND actual_worth > 0

    GROUP BY
        area_en,
        prop_type_en
),

rents AS (
    SELECT
        area_en,
        prop_type_en,

        COUNT(*) AS rent_deals,

        AVG(NULLIF(annual_amount, 0)) AS avg_annual_rent

    FROM dld_rents_full

    WHERE annual_amount IS NOT NULL
      AND annual_amount > 0

    GROUP BY
        area_en,
        prop_type_en
)

SELECT
    s.area_en,
    s.prop_type_en,

    s.sales_deals,
    r.rent_deals,

    ROUND(s.avg_sale_price::numeric, 2) AS avg_sale_price,
    ROUND(s.avg_meter_sale_price::numeric, 2) AS avg_meter_sale_price,
    ROUND(s.avg_area::numeric, 2) AS avg_area,

    ROUND(r.avg_annual_rent::numeric, 2) AS avg_annual_rent,

    CASE
        WHEN s.avg_sale_price > 0
         AND r.avg_annual_rent IS NOT NULL
        THEN ROUND(
            ((r.avg_annual_rent / s.avg_sale_price) * 100)::numeric,
            2
        )
        ELSE NULL
    END AS gross_yield_percent,

    NOW() AS updated_at

FROM sales s

LEFT JOIN rents r
ON LOWER(TRIM(COALESCE(s.area_en, ''))) =
   LOWER(TRIM(COALESCE(r.area_en, '')))

AND LOWER(TRIM(COALESCE(s.prop_type_en, ''))) =
    LOWER(TRIM(COALESCE(r.prop_type_en, '')));
""")

conn.commit()

cur.execute("SELECT COUNT(*) FROM roi_analytics;")
count = cur.fetchone()[0]

print(f"ROI TABLE CREATED: {count} ROWS", flush=True)

cur.close()
conn.close()

print("DONE", flush=True)
