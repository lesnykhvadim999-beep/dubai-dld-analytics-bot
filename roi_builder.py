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
        LOWER(TRIM(area_en)) AS area_key,
        MIN(area_en) AS sales_area,

        COUNT(*) AS sales_deals,

        AVG(NULLIF(procedure_area, 0)) AS avg_area

    FROM dld_transactions_full

    WHERE area_en IS NOT NULL
      AND procedure_area IS NOT NULL
      AND procedure_area > 0

    GROUP BY LOWER(TRIM(area_en))
),

rents AS (
    SELECT
        LOWER(TRIM(area_en)) AS area_key,
        MIN(area_en) AS rent_area,

        COUNT(*) AS rent_deals,

        AVG(NULLIF(annual_amount, 0)) AS avg_annual_rent

    FROM dld_rents_full

    WHERE area_en IS NOT NULL
      AND annual_amount IS NOT NULL
      AND annual_amount > 0

    GROUP BY LOWER(TRIM(area_en))
)

SELECT
    s.sales_area,
    r.rent_area,

    s.sales_deals,
    r.rent_deals,

    ROUND(s.avg_area::numeric, 2) AS avg_area,
    ROUND(r.avg_annual_rent::numeric, 2) AS avg_annual_rent,

    NULL::numeric AS avg_property_price,
    NULL::numeric AS gross_yield_percent,

    NOW() AS updated_at

FROM sales s
JOIN rents r
ON s.area_key = r.area_key;
""")

conn.commit()

cur.execute("SELECT COUNT(*) FROM roi_analytics;")
count = cur.fetchone()[0]

print(f"ROI TABLE CREATED: {count} ROWS", flush=True)

cur.close()
conn.close()

print("DONE", flush=True)
