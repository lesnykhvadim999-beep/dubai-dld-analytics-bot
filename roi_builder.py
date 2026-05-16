import os
import psycopg2

DATABASE_URL = os.getenv("DATABASE_URL")

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

print("CONNECTED")

cur.execute("""
DROP TABLE IF EXISTS roi_analytics;

CREATE TABLE roi_analytics AS

WITH sales AS (
    SELECT
        area_en,
        COUNT(*) AS sales_deals,
        AVG(actual_worth) AS avg_property_price,
        AVG(procedure_area) AS avg_area
    FROM dld_transactions_full
    WHERE actual_worth IS NOT NULL
      AND actual_worth > 0
      AND procedure_area IS NOT NULL
      AND procedure_area > 0
      AND area_en IS NOT NULL
    GROUP BY area_en
),

rents AS (
    SELECT
        area_en,
        COUNT(*) AS rent_deals,
        AVG(annual_amount) AS avg_annual_rent
    FROM dld_rents_full
    WHERE annual_amount IS NOT NULL
      AND annual_amount > 0
      AND area_en IS NOT NULL
    GROUP BY area_en
)

SELECT
    s.area_en,
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
ON LOWER(TRIM(s.area_en)) = LOWER(TRIM(r.area_en))

WHERE r.avg_annual_rent > 0
  AND s.avg_property_price > 0;
""")

conn.commit()

cur.execute("SELECT COUNT(*) FROM roi_analytics;")
count = cur.fetchone()[0]

print(f"ROI TABLE CREATED: {count} ROWS")

cur.close()
conn.close()

print("DONE")
