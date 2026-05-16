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
        LOWER(TRIM(COALESCE(area_en, 'UNKNOWN'))) AS area_key,

        MIN(area_en) AS sales_area,

        COUNT(*) AS sales_deals,

        AVG(
            CASE
                WHEN procedure_area > 0
                THEN procedure_area
            END
        ) AS avg_area

    FROM dld_transactions_full

    GROUP BY LOWER(TRIM(COALESCE(area_en, 'UNKNOWN')))
),

rents AS (
    SELECT
        LOWER(TRIM(COALESCE(area_en, 'UNKNOWN'))) AS area_key,

        MIN(area_en) AS rent_area,

        COUNT(*) AS rent_deals,

        AVG(
            CASE
                WHEN annual_amount > 0
                THEN annual_amount
            END
        ) AS avg_annual_rent

    FROM dld_rents_full

    GROUP BY LOWER(TRIM(COALESCE(area_en, 'UNKNOWN')))
)

SELECT
    COALESCE(s.sales_area, r.rent_area) AS area_name,

    s.sales_deals,
    r.rent_deals,

    ROUND(s.avg_area::numeric, 2) AS avg_area,

    ROUND(r.avg_annual_rent::numeric, 2) AS avg_annual_rent,

    NOW() AS updated_at

FROM sales s
FULL OUTER JOIN rents r
ON s.area_key = r.area_key

WHERE
    s.sales_area IS NOT NULL
    OR r.rent_area IS NOT NULL;
""")

conn.commit()

cur.execute("SELECT COUNT(*) FROM roi_analytics;")
count = cur.fetchone()[0]

print(f"ROI TABLE CREATED: {count} ROWS", flush=True)

cur.close()
conn.close()

print("DONE", flush=True)
