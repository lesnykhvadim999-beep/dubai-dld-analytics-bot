import os
import time
import requests
import psycopg2
from psycopg2.extras import execute_values

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

API_URL = "https://gateway.dubailand.gov.ae/open-data/transactions"

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

print("CONNECTED TO DB", flush=True)

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

print("TABLE READY", flush=True)


def safe_float(value):
    try:
        if value is None:
            return None

        if value == "":
            return None

        return float(value)

    except:
        return None


def getv(item, *keys):

    for key in keys:

        if key in item and item.get(key) is not None:
            return item.get(key)

        lower_key = key.lower()

        if lower_key in item and item.get(lower_key) is not None:
            return item.get(lower_key)

    return None


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

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://dubailand.gov.ae",
        "Referer": "https://dubailand.gov.ae/"
    }

    print(f"FETCHING SALES skip={skip}", flush=True)

    response = requests.post(
        API_URL,
        json=payload,
        headers=headers,
        timeout=90
    )

    print(f"STATUS={response.status_code}", flush=True)

    response.raise_for_status()

    return response.json()


def extract_rows(data):

    response = data.get("response", {})

    if isinstance(response, dict):

        for key in ["result", "data", "items"]:

            if isinstance(response.get(key), list):
                return response[key]

    for key in ["result", "data", "items"]:

        if isinstance(data.get(key), list):
            return data[key]

    return []


def save_transactions(rows):

    if not rows:
        return

    values = []

    seen_ids = set()

    for item in rows:

        transaction_id = (
            getv(item, "TRANSACTION_ID", "transaction_id")
            or getv(item, "TRANSACTION_NUMBER", "transaction_number")
            or getv(item, "INSTANCE_ID", "instance_id")
        )

        if not transaction_id:
            continue

        if transaction_id in seen_ids:
            continue

        seen_ids.add(transaction_id)

        actual_area = safe_float(
            getv(
                item,
                "ACTUAL_AREA",
                "actual_area",
                "PROCEDURE_AREA",
                "procedure_area"
            )
        )

        values.append((
            str(transaction_id),

            getv(item, "TRANSACTION_NUMBER", "transaction_number"),

            getv(
                item,
                "INSTANCE_DATE",
                "instance_date",
                "TRANSACTION_DATE",
                "transaction_date"
            ),

            getv(
                item,
                "PROCEDURE_NAME_EN",
                "procedure_name_en",
                "PROCEDURE_NAME",
                "procedure_name"
            ),

            getv(item, "AREA_ID", "area_id"),

            getv(item, "AREA_EN", "area_en"),

            getv(item, "AREA_AR", "area_ar"),

            getv(
                item,
                "PROJECT_EN",
                "project_en",
                "MASTER_PROJECT_EN",
                "master_project_en"
            ),

            getv(
                item,
                "PROJECT_AR",
                "project_ar",
                "MASTER_PROJECT_AR",
                "master_project_ar"
            ),

            getv(item, "BUILDING_EN", "building_en"),

            getv(item, "BUILDING_AR", "building_ar"),

            getv(
                item,
                "PROP_TYPE_EN",
                "prop_type_en",
                "PROPERTY_TYPE_EN",
                "property_type_en"
            ),

            getv(
                item,
                "PROP_SB_TYPE_EN",
                "prop_sb_type_en",
                "PROP_SUB_TYPE_EN",
                "prop_sub_type_en"
            ),

            getv(item, "ROOMS_EN", "rooms_en"),

            safe_float(
                getv(
                    item,
                    "ACTUAL_WORTH",
                    "actual_worth",
                    "AMOUNT",
                    "amount",
                    "VALUE",
                    "value"
                )
            ),

            safe_float(
                getv(
                    item,
                    "METER_SALE_PRICE",
                    "meter_sale_price"
                )
            ),

            actual_area,

            safe_float(
                getv(
                    item,
                    "PROCEDURE_AREA",
                    "procedure_area"
                )
            ),

            getv(item, "PARKING", "parking"),

            getv(item, "NEAREST_METRO_EN", "nearest_metro_en"),

            getv(item, "NEAREST_MALL_EN", "nearest_mall_en"),

            getv(item, "NEAREST_LANDMARK_EN", "nearest_landmark_en"),

            getv(item, "USAGE_ID", "usage_id"),

            str(getv(item, "IS_FREE_HOLD", "is_free_hold"))
            if getv(item, "IS_FREE_HOLD", "is_free_hold") is not None
            else None,

            str(getv(item, "IS_OFFPLAN", "is_offplan"))
            if getv(item, "IS_OFFPLAN", "is_offplan") is not None
            else None
        ))

    if not values:
        print("NO VALID SALES ROWS TO SAVE", flush=True)
        return

    print(f"SAVING SALES {len(values)} ROWS", flush=True)

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

        ON CONFLICT (transaction_id)
        DO UPDATE SET

            actual_worth =
                COALESCE(
                    EXCLUDED.actual_worth,
                    dld_transactions_full.actual_worth
                ),

            meter_sale_price =
                COALESCE(
                    EXCLUDED.meter_sale_price,
                    dld_transactions_full.meter_sale_price
                ),

            actual_area =
                COALESCE(
                    EXCLUDED.actual_area,
                    dld_transactions_full.actual_area
                ),

            procedure_area =
                COALESCE(
                    EXCLUDED.procedure_area,
                    dld_transactions_full.procedure_area
                )
        """,
        values
    )

    conn.commit()

    print("COMMIT DONE", flush=True)


def refresh_sales_analytics():

    print("REFRESHING SALES ANALYTICS", flush=True)

    cur.execute("""
    DROP TABLE IF EXISTS sales_analytics_by_building;

    CREATE TABLE sales_analytics_by_building AS

    SELECT

        area_en,
        project_en,
        building_en,
        prop_type_en,
        prop_sub_type_en,
        rooms_en,

        COUNT(*) AS deals_count,

        ROUND(
            AVG(
                COALESCE(actual_worth, 0)
            ),
            2
        ) AS avg_sale_price,

        ROUND(
            AVG(
                COALESCE(meter_sale_price, 0)
            ),
            2
        ) AS avg_meter_sale_price,

        ROUND(
            AVG(
                COALESCE(actual_area, procedure_area, 0)
            ),
            2
        ) AS avg_area,

        MIN(transaction_date) AS first_transaction,
        MAX(transaction_date) AS last_transaction,

        NOW() AS updated_at

    FROM dld_transactions_full

    GROUP BY
        area_en,
        project_en,
        building_en,
        prop_type_en,
        prop_sub_type_en,
        rooms_en;
    """)

    conn.commit()

    print("SALES ANALYTICS READY", flush=True)


def run_parser(from_date, to_date):

    skip = 0
    take = 1000
    total = 0

    while True:

        data = fetch_transactions(
            from_date=from_date,
            to_date=to_date,
            skip=skip,
            take=take
        )

        rows = extract_rows(data)

        print(f"RECEIVED SALES: {len(rows)}", flush=True)

        if not rows:
            print("NO MORE SALES ROWS", flush=True)
            break

        save_transactions(rows)

        total += len(rows)

        print(f"TOTAL SALES SAVED: {total}", flush=True)

        if len(rows) < take:
            print("LAST SALES PAGE REACHED", flush=True)
            break

        skip += take

        time.sleep(1)

    refresh_sales_analytics()

    print("DONE", flush=True)

    cur.close()
    conn.close()


if __name__ == "__main__":
    # Dynamic rolling window: last DLD_LOOKBACK_DAYS days up to today.
    # DLD upstream usually lags by 3-7 days, so 30 days catches everything new.
    from datetime import datetime, timedelta

    lookback = int(os.getenv("DLD_LOOKBACK_DAYS", "30"))
    today = datetime.utcnow().date()
    from_date_dt = today - timedelta(days=lookback)

    from_date_str = from_date_dt.strftime("%m/%d/%Y")
    to_date_str = today.strftime("%m/%d/%Y")

    print(f"DLD WINDOW: {from_date_str} → {to_date_str}", flush=True)

    run_parser(
        from_date=from_date_str,
        to_date=to_date_str,
    )
