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

SELECT
    s.area_en,

    s.sales_deals,

    r.rent_deals,

    ROUND(s.avg_sale_price::numeric, 2) AS avg_sale_price,
    ROUND(r.avg_annual_rent::numeric, 2) AS avg_annual_rent,

    CASE
        WHEN s.avg_sale_price > 0
         AND r.avg_annual_rent > 0
        THEN ROUND(
            ((r.avg_annual_rent / s.avg_sale_price) * 100)::numeric,
            2
        )
        ELSE NULL
    END AS gross_yield_percent,

    NOW() AS updated_at

FROM (

    SELECT
        area_en,

        COUNT(*) AS sales_deals,

        AVG(actual_worth) AS avg_sale_price

    FROM dld_transactions_full

    WHERE actual_worth > 0

    GROUP BY area_en

) s

LEFT JOIN (

    SELECT
        area_en,

        COUNT(*) AS rent_deals,

        AVG(annual_amount) AS avg_annual_rent

    FROM dld_rents_full

    WHERE annual_amount > 0

    GROUP BY area_en

) r

ON s.area_en = r.area_en;
""")

conn.commit()

cur.execute("SELECT COUNT(*) FROM roi_analytics;")
count = cur.fetchone()[0]

print(f"ROI TABLE CREATED: {count} ROWS", flush=True)

cur.close()
conn.close()

print("DONE", flush=True)
