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

        AVG(
            NULLIF(meter_sale_price, 0)
        ) AS avg_sale_price_psf,

        AVG(
            NULLIF(procedure_area, 0)
        ) AS avg_area,

        COUNT(*) AS sales_deals

    FROM dld_transactions_full

    WHERE meter_sale_price IS NOT NULL
      AND meter_sale_price > 0

    GROUP BY area_en
),

rents AS (
    SELECT
        area_en,

        AVG(
            NULLIF(annual_amount, 0)
        ) AS avg_annual_rent,

        COUNT(*) AS rent_deals

    FROM dld_rents_full

    WHERE annual_amount IS NOT NULL
      AND annual_amount > 0

    GROUP BY area_en
)

SELECT
    s.area_en,

    s.avg_sale_price_psf,
    s.avg_area,

    r.avg_annual_rent,

    s.sales_deals,
    r.rent_deals,

    ROUND(
        (
            r.avg_annual_rent /
            NULLIF(
                s.avg_sale_price_psf * s.avg_area,
                0
            )
        ) * 100,
        2
    ) AS gross_yield_percent

FROM sales s

JOIN rents r
ON LOWER(TRIM(COALESCE(s.area_en, ''))) =
   LOWER(TRIM(COALESCE(r.area_en, '')))

WHERE r.avg_annual_rent IS NOT NULL
  AND s.avg_sale_price_psf IS NOT NULL
  AND s.avg_area IS NOT NULL;

""")

conn.commit()

cur.execute("SELECT COUNT(*) FROM roi_analytics;")
count = cur.fetchone()[0]

print(f"ROI TABLE CREATED: {count} ROWS")

cur.close()
conn.close()

print("DONE")
