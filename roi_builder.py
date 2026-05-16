import os
import psycopg2

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

print("CONNECTED", flush=True)


def get_columns(table_name):
    cur.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = %s
    """, (table_name,))
    return {row[0] for row in cur.fetchall()}


sales_cols = get_columns("dld_transactions_full")
rent_cols = get_columns("dld_rents_full")


def col(table_alias, columns, name, fallback="NULL::text"):
    if name in columns:
        return f"{table_alias}.{name}"
    return fallback


sales_area = col("t", sales_cols, "area_en")
sales_project = col("t", sales_cols, "project_en")
sales_building = col("t", sales_cols, "building_en")
sales_prop_type = col("t", sales_cols, "prop_type_en")
sales_prop_sub_type = col("t", sales_cols, "prop_sub_type_en")
sales_rooms = col("t", sales_cols, "rooms_en")

rent_area = col("r", rent_cols, "area_en")
rent_project = col("r", rent_cols, "project_en")
rent_building = col("r", rent_cols, "building_en")
rent_prop_type = col("r", rent_cols, "prop_type_en")
rent_prop_sub_type = col("r", rent_cols, "prop_sub_type_en")
rent_rooms = col("r", rent_cols, "rooms_en")

sql = f"""
DROP TABLE IF EXISTS roi_analytics;

CREATE TABLE roi_analytics AS

WITH sales AS (
    SELECT
        {sales_area} AS area_en,
        {sales_project} AS project_en,
        {sales_building} AS building_en,
        {sales_prop_type} AS prop_type_en,
        {sales_prop_sub_type} AS prop_sub_type_en,
        {sales_rooms} AS rooms_en,

        COUNT(*) AS sales_deals,
        AVG(NULLIF(t.actual_worth, 0)) AS avg_sale_price,
        AVG(NULLIF(t.meter_sale_price, 0)) AS avg_meter_sale_price,
        AVG(NULLIF(t.actual_area, 0)) AS avg_area

    FROM dld_transactions_full t
    WHERE t.actual_worth IS NOT NULL
      AND t.actual_worth > 0

    GROUP BY
        {sales_area},
        {sales_project},
        {sales_building},
        {sales_prop_type},
        {sales_prop_sub_type},
        {sales_rooms}
),

rents AS (
    SELECT
        {rent_area} AS area_en,
        {rent_project} AS project_en,
        {rent_building} AS building_en,
        {rent_prop_type} AS prop_type_en,
        {rent_prop_sub_type} AS prop_sub_type_en,
        {rent_rooms} AS rooms_en,

        COUNT(*) AS rent_deals,
        AVG(NULLIF(r.annual_amount, 0)) AS avg_annual_rent

    FROM dld_rents_full r
    WHERE r.annual_amount IS NOT NULL
      AND r.annual_amount > 0

    GROUP BY
        {rent_area},
        {rent_project},
        {rent_building},
        {rent_prop_type},
        {rent_prop_sub_type},
        {rent_rooms}
)

SELECT
    s.area_en,
    s.project_en,
    s.building_en,
    s.prop_type_en,
    s.prop_sub_type_en,
    s.rooms_en,

    s.sales_deals,
    r.rent_deals,

    ROUND(s.avg_sale_price::numeric, 2) AS avg_sale_price,
    ROUND(s.avg_meter_sale_price::numeric, 2) AS avg_meter_sale_price,
    ROUND(s.avg_area::numeric, 2) AS avg_area,
    ROUND(r.avg_annual_rent::numeric, 2) AS avg_annual_rent,

    CASE
        WHEN s.avg_sale_price > 0
         AND r.avg_annual_rent IS NOT NULL
        THEN ROUND(((r.avg_annual_rent / s.avg_sale_price) * 100)::numeric, 2)
        ELSE NULL
    END AS gross_yield_percent,

    NOW() AS updated_at

FROM sales s
LEFT JOIN rents r
ON LOWER(TRIM(COALESCE(s.area_en, ''))) = LOWER(TRIM(COALESCE(r.area_en, '')))
AND LOWER(TRIM(COALESCE(s.prop_type_en, ''))) = LOWER(TRIM(COALESCE(r.prop_type_en, '')))
AND LOWER(TRIM(COALESCE(s.prop_sub_type_en, ''))) = LOWER(TRIM(COALESCE(r.prop_sub_type_en, '')))
AND LOWER(TRIM(COALESCE(s.rooms_en, ''))) = LOWER(TRIM(COALESCE(r.rooms_en, '')));
"""

cur.execute(sql)
conn.commit()

cur.execute("SELECT COUNT(*) FROM roi_analytics;")
count = cur.fetchone()[0]

print(f"ROI TABLE CREATED: {count} ROWS", flush=True)

cur.close()
conn.close()

print("DONE", flush=True)
