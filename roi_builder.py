import os
import psycopg2

DATABASE_URL = os.getenv("DATABASE_URL")

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

print("CONNECTED")

cur.execute("""
DROP TABLE IF EXISTS roi_analytics;

CREATE TABLE roi_analytics AS
SELECT
    s.area_en,
    s.project_en,
    s.building_en,
    s.prop_type_en,
    s.prop_sub_type_en,
    s.rooms_en,

    s.deals_count AS sales_deals,
    r.deals_count AS rent_deals,

    s.avg_sale_price,
    s.avg_meter_sale_price,
    s.avg_area,

    r.avg_annual_rent,
    r.avg_contract,

    CASE
        WHEN s.avg_sale_price > 0
         AND r.avg_annual_rent IS NOT NULL
        THEN ROUND(
            (r.avg_annual_rent / s.avg_sale_price) * 100,
            2
        )
        ELSE NULL
    END AS gross_yield_percent,

    NOW() AS updated_at

FROM sales_analytics_by_building s
LEFT JOIN rent_analytics_by_building r
ON LOWER(TRIM(s.area_en)) = LOWER(TRIM(r.area_en));
""")

conn.commit()

cur.execute("SELECT COUNT(*) FROM roi_analytics;")
count = cur.fetchone()[0]

print(f"ROI TABLE CREATED: {count} ROWS")

cur.close()
conn.close()

print("DONE")
