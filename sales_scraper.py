import os
import time
import requests
import psycopg2
from psycopg2.extras import execute_values

DATABASE_URL = os.getenv("DATABASE_URL")
API_URL = "[https://gateway.dubailand.gov.ae/open-data/transactions](https://gateway.dubailand.gov.ae/open-data/transactions)"

if not DATABASE_URL:
raise RuntimeError("DATABASE_URL is not set")

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS dld_transactions_full (
transaction_id TEXT PRIMARY KEY,
transaction_number TEXT,
transaction_date TEXT,
procedure_name TEXT,
area_id INTEGER,
area_en TEXT,
area_ar TEXT,
project_en TEXT,
project_ar TEXT,
building_en TEXT,
building_ar TEXT,
prop_type_en TEXT,
prop_sub_type_en TEXT,
rooms_en TEXT,
actual_worth NUMERIC,
meter_sale_price NUMERIC,
actual_area NUMERIC,
procedure_area NUMERIC,
parking TEXT,
nearest_metro_en TEXT,
nearest_mall_en TEXT,
nearest_landmark_en TEXT,
usage_id INTEGER,
is_free_hold TEXT,
is_offplan TEXT,
created_at TIMESTAMP DEFAULT NOW()
);
""")

conn.commit()

def fetch_transactions(from_date, to_date, skip=0, take=1000):
payload = {
"P_FROM_DATE": from_date,
"P_TO_DATE": to_date,
"P_GROUP_ID": "",
"P_IS_OFFPLAN": "",
"P_AREA_ID": "",
"P_IS_FREE_HOLD": "",
"P_PROP_TYPE_ID": "",
"P_SKIP": str(skip),
"P_SORT": "TRANSACTION_NUMBER_ASC",
"P_TAKE": str(take),
"P_USAGE_ID": ""
}

```
headers = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://dubailand.gov.ae",
    "Referer": "https://dubailand.gov.ae/"
}

response = requests.post(
    API_URL,
    json=payload,
    headers=headers,
    timeout=90
)

response.raise_for_status()
return response.json()
```

def extract_rows(data):
response = data.get("response", {})

```
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
```

def save_transactions(rows):
if not rows:
return

```
values = []

for item in rows:
    transaction_id = (
        item.get("TRANSACTION_ID")
        or item.get("TRANSACTION_NUMBER")
        or item.get("INSTANCE_ID")
    )

    if not transaction_id:
        continue

    values.append((
        str(transaction_id),
        item.get("TRANSACTION_NUMBER"),
        item.get("INSTANCE_DATE"),
        item.get("PROCEDURE_NAME_EN"),
        item.get("AREA_ID"),
        item.get("AREA_EN"),
        item.get("AREA_AR"),
        item.get("PROJECT_EN"),
        item.get("PROJECT_AR"),
        item.get("BUILDING_EN"),
        item.get("BUILDING_AR"),
        item.get("PROP_TYPE_EN"),
        item.get("PROP_SB_TYPE_EN"),
        item.get("ROOMS_EN"),
        item.get("ACTUAL_WORTH"),
        item.get("METER_SALE_PRICE"),
        item.get("ACTUAL_AREA"),
        item.get("PROCEDURE_AREA"),
        item.get("PARKING"),
        item.get("NEAREST_METRO_EN"),
        item.get("NEAREST_MALL_EN"),
        item.get("NEAREST_LANDMARK_EN"),
        item.get("USAGE_ID"),
        str(item.get("IS_FREE_HOLD")) if item.get("IS_FREE_HOLD") is not None else None,
        str(item.get("IS_OFFPLAN")) if item.get("IS_OFFPLAN") is not None else None
    ))

if not values:
    return

execute_values(
    cur,
    """
    INSERT INTO dld_transactions_full (
        transaction_id,
        transaction_number,
        transaction_date,
        procedure_name,
        area_id,
        area_en,
        area_ar,
        project_en,
        project_ar,
        building_en,
        building_ar,
        prop_type_en,
        prop_sub_type_en,
        rooms_en,
        actual_worth,
        meter_sale_price,
        actual_area,
        procedure_area,
        parking,
        nearest_metro_en,
        nearest_mall_en,
        nearest_landmark_en,
        usage_id,
        is_free_hold,
        is_offplan
    )
    VALUES %s
    ON CONFLICT (transaction_id) DO NOTHING
    """,
    values
)

conn.commit()
```

def run_parser(from_date, to_date):
skip = 0
take = 1000
total = 0

```
while True:
    print(f"Fetching skip={skip}")

    data = fetch_transactions(
        from_date=from_date,
        to_date=to_date,
        skip=skip,
        take=take
    )

    rows = extract_rows(data)

    print(f"Received: {len(rows)}")

    if not rows:
        print("Finished.")
        break

    save_transactions(rows)

    total += len(rows)
    print(f"Saved total: {total}")

    if len(rows) < take:
        print("Last page reached.")
        break

    skip += take
    time.sleep(1)

cur.close()
conn.close()

print("DONE")
```

if **name** == "**main**":
run_parser(
from_date="05/01/2026",
to_date="05/15/2026"
)
