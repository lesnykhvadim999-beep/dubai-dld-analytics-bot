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
        property_type_en,

        AVG(
            CASE
                WHEN procedure_area > 0
                THEN actual_worth / procedure_area
                ELSE NULL
            END
        ) AS avg_sale_price,

        AVG(actual_worth) AS avg_property_price

    FROM dld_transactions_full
    WHERE actual_worth IS NOT NULL
      AND actual_worth > 0
      AND procedure_area IS NOT NULL
      AND procedure_area > 0
      AND area_en IS NOT NULL

    GROUP BY area_en, property_type_en
),

rents AS (
    SELECT
        area_en,
        property_type_en,

        AVG(
            CASE
                WHEN procedure_area > 0
                THEN annual_amount / procedure_area
                ELSE NULL
            END
        ) AS avg_rent_price,

        AVG(annual_amount) AS avg_annual_rent

    FROM dld_rents_full
    WHERE annual_amount IS NOT NULL
      AND annual_amount > 0
      AND procedure_area IS NOT NULL
      AND procedure_area > 0
      AND area_en IS NOT NULL

    GROUP BY area_en, property_type_en
)

SELECT
    s.area_en,
    s.property_type_en,

    ROUND(s.avg_sale_price::numeric, 2) AS avg_sale_price,
    ROUND(r.avg_rent_price::numeric, 2) AS avg_rent_price,

    ROUND(s.avg_property_price::numeric, 2) AS avg_property_price,
    ROUND(r.avg_annual_rent::numeric, 2) AS avg_annual_rent,

    ROUND(
        (
            r.avg_annual_rent /
            NULLIF(s.avg_property_price, 0)
        ) * 100
    , 2) AS gross_yield_percent

FROM sales s
JOIN rents r
ON LOWER(TRIM(s.area_en)) =
   LOWER(TRIM(r.area_en))

AND LOWER(TRIM(COALESCE(s.property_type_en, ''))) =
    LOWER(TRIM(COALESCE(r.property_type_en, '')))

WHERE s.avg_property_price > 0
  AND r.avg_annual_rent > 0;

""")

conn.commit()

cur.execute("""
SELECT COUNT(*)
FROM roi_analytics;
""")

count = cur.fetchone()[0]

print(f"ROI TABLE CREATED: {count} ROWS")

cur.close()
conn.close()

print("DONE")
