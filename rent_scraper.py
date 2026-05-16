import os
import time
import hashlib
from datetime import datetime, timedelta, timezone

import requests
import psycopg2
from psycopg2.extras import execute_values, Json

DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("RENT_DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL / RENT_DATABASE_URL is not set")

API_URL = "https://gateway.dubailand.gov.ae/open-data/rents"


def default_dates():
    dubai_tz = timezone(timedelta(hours=4))
    today = datetime.now(dubai_tz).date()
    from_date = today - timedelta(days=3)
    return from_date.strftime("%m/%d/%Y"), today.strftime("%m/%d/%Y")


FROM_DATE = os.getenv("FROM_DATE")
TO_DATE = os.getenv("TO_DATE")

if not FROM_DATE or not TO_DATE:
    FROM_DATE, TO_DATE = default_dates()

print(f"FROM_DATE={FROM_DATE}", flush=True)
print(f"TO_DATE={TO_DATE}", flush=True)

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

print("CONNECTED TO DB", flush=True)

cur.execute("""
CREATE TABLE IF NOT EXISTS dld_rents_full (
    rent_id TEXT PRIMARY KEY,
    rn INTEGER,
    total INTEGER,
    total_properties INTEGER,
    registration_date TEXT,
    start_date TEXT,
    end_date TEXT,
    contract_amount NUMERIC,
    annual_amount NUMERIC,
    actual_area NUMERIC,
    area_id INTEGER,
    area_en TEXT,
    area_ar TEXT,
    project_en TEXT,
    project_ar TEXT,
    prop_type_en TEXT,
    prop_type_ar TEXT,
    prop_sub_type_en TEXT,
    prop_sub_type_ar TEXT,
    usage_en TEXT,
    usage_ar TEXT,
    nearest_metro_en TEXT,
    nearest_mall_en TEXT,
    nearest_landmark_en TEXT,
    raw_json JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);
""")

conn.commit()
print("TABLE READY", flush=True)


def make_rent_id(item):
    raw = "|".join([
        str(item.get("REGISTRATION_DATE")),
        str(item.get("AREA_EN")),
        str(item.get("PROJECT_EN")),
        str(item.get("ANNUAL_AMOUNT")),
        str(item.get("ACTUAL_AREA")),
    ])
    return hashlib.md5(raw.encode()).hexdigest()


def fetch_rents(skip=0, take=1000):
    payload = {
        "P_DATE_TYPE": "0",
        "P_FROM_DATE": FROM_DATE,
        "P_TO_DATE": TO_DATE,
        "P_IS_FREE_HOLD": "",
        "P_VERSION": "",
        "P_AREA_ID": "",
        "P_USAGE_ID": "",
        "P_PROP_TYPE_ID": "",
        "P_TAKE": str(take),
        "P_SKIP": str(skip),
        "P_SORT": "REGISTRATION_DATE_ASC",
    }

    print(f"FETCHING skip={skip}", flush=True)

    response = requests.post(API_URL, json=payload, timeout=90)
    print(f"STATUS={response.status_code}", flush=True)

    response.raise_for_status()
    return response.json()


def extract_rows(data):
    response = data.get("response", {})

    if isinstance(response, dict):
        if isinstance(response.get("result"), list):
            return response["result"]
        if isinstance(response.get("data"), list):
            return response["data"]
        if isinstance(response.get("items"), list):
            return response["items"]

    if isinstance(data.get("result"), list):
        return data["result"]

    if isinstance(data.get("data"), list):
        return data["data"]

    return []


def save_rows(rows):
    if not rows:
        return

    values = []

    for item in rows:
        values.append((
            make_rent_id(item),
            item.get("RN"),
            item.get("TOTAL"),
            item.get("TOTAL_PROPERTIES"),
            item.get("REGISTRATION_DATE"),
            item.get("START_DATE"),
            item.get("END_DATE"),
            item.get("CONTRACT_AMOUNT"),
            item.get("ANNUAL_AMOUNT"),
            item.get("ACTUAL_AREA"),
            item.get("AREA_ID"),
            item.get("AREA_EN"),
            item.get("AREA_AR"),
            item.get("PROJECT_EN"),
            item.get("PROJECT_AR"),
            item.get("PROP_TYPE_EN"),
            item.get("PROP_TYPE_AR"),
            item.get("PROP_SUB_TYPE_EN"),
            item.get("PROP_SUB_TYPE_AR"),
            item.get("USAGE_EN"),
            item.get("USAGE_AR"),
            item.get("NEAREST_METRO_EN"),
            item.get("NEAREST_MALL_EN"),
            item.get("NEAREST_LANDMARK_EN"),
            Json(item),
        ))

    print(f"SAVING {len(values)} ROWS", flush=True)

    execute_values(
        cur,
        """
        INSERT INTO dld_rents_full (
            rent_id,
            rn,
            total,
            total_properties,
            registration_date,
            start_date,
            end_date,
            contract_amount,
            annual_amount,
            actual_area,
            area_id,
            area_en,
            area_ar,
            project_en,
            project_ar,
            prop_type_en,
            prop_type_ar,
            prop_sub_type_en,
            prop_sub_type_ar,
            usage_en,
            usage_ar,
            nearest_metro_en,
            nearest_mall_en,
            nearest_landmark_en,
            raw_json
        )
        VALUES %s
        ON CONFLICT (rent_id) DO NOTHING
        """,
        values,
    )

    conn.commit()
    print("COMMIT DONE", flush=True)


def refresh_rent_analytics():
    print("REFRESHING RENT ANALYTICS", flush=True)

    cur.execute("""
    DROP TABLE IF EXISTS rent_analytics_by_building;

    CREATE TABLE rent_analytics_by_building AS
    SELECT
        area_en,
        project_en,
        prop_type_en,
        prop_sub_type_en,

        COUNT(*) AS deals_count,

        ROUND(AVG(annual_amount), 2) AS avg_annual_rent,
        ROUND(AVG(contract_amount), 2) AS avg_contract_amount,
        ROUND(AVG(actual_area), 2) AS avg_area,

        ROUND(
            AVG(
                annual_amount / NULLIF(actual_area, 0)
            ),
            2
        ) AS avg_rent_per_sqft,

        MIN(registration_date) AS first_contract,
        MAX(registration_date) AS last_contract,

        NOW() AS updated_at

    FROM dld_rents_full

    WHERE annual_amount IS NOT NULL
    AND actual_area IS NOT NULL
    AND actual_area > 0

    GROUP BY
        area_en,
        project_en,
        prop_type_en,
        prop_sub_type_en;
    """)

    conn.commit()
    print("RENT ANALYTICS READY", flush=True)


skip = 0
take = 1000
total = 0

while True:
    data = fetch_rents(skip, take)

    rows = extract_rows(data)

    print(f"RECEIVED {len(rows)} ROWS", flush=True)

    if not rows:
        print("NO MORE ROWS", flush=True)
        break

    save_rows(rows)

    total += len(rows)

    print(f"TOTAL SAVED: {total}", flush=True)

    if len(rows) < take:
        print("LAST PAGE REACHED", flush=True)
        break

    skip += take
    time.sleep(1)

refresh_rent_analytics()

print("DONE", flush=True)

cur.close()
conn.close()
