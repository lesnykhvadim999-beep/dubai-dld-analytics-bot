import os
import time
import hashlib
from datetime import datetime, timedelta, timezone

import requests
import psycopg2
from psycopg2.extras import execute_values

DATABASE_URL = os.getenv("RENT_DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("RENT_DATABASE_URL is not set")

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

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

cur.execute('''
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
''')

conn.commit()

def make_rent_id(item):
    raw = "|".join([
        str(item.get("REGISTRATION_DATE")),
        str(item.get("AREA_EN")),
        str(item.get("PROJECT_EN")),
        str(item.get("ANNUAL_AMOUNT")),
        str(item.get("ACTUAL_AREA"))
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
        "P_SORT": "REGISTRATION_DATE_ASC"
    }

    r = requests.post(API_URL, json=payload, timeout=90)
    r.raise_for_status()
    return r.json()

def save_rows(rows):
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
            item
        ))

    execute_values(
        cur,
        '''
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
        ''',
        values
    )

    conn.commit()

skip = 0
take = 1000
total = 0

while True:
    print(f"Fetching skip={skip}")

    data = fetch_rents(skip, take)

    rows = data.get("response", {}).get("result", [])

    print(f"Received: {len(rows)}")

    if not rows:
        break

    save_rows(rows)

    total += len(rows)

    print(f"Saved total: {total}")

    if len(rows) < take:
        print("Last page reached")
        break

    skip += take
    time.sleep(1)

print("DONE")

cur.close()
conn.close()
