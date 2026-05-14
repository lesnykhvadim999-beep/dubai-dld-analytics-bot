import requests
import pandas as pd
from sqlalchemy import create_engine
from datetime import datetime, timedelta
import os

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

engine = create_engine(DATABASE_URL)

cutoff_date = datetime.now() - timedelta(days=90)

url = "https://www.dubaipulse.gov.ae/api/dataset/download/json"

params = {
    "dataset": "rental-contracts"
}

headers = {
    "User-Agent": "Mozilla/5.0"
}

print("Downloading data from Dubai Pulse...")

response = requests.get(
    url,
    params=params,
    headers=headers,
    timeout=120
)

print("Status:", response.status_code)

if response.status_code != 200:
    print("RESPONSE TEXT:")
    print(response.text)
    raise RuntimeError(f"Request failed: {response.status_code}")

try:
    data = response.json()
except Exception:
    print("RESPONSE TEXT:")
    print(response.text)
    raise

rows = []

for item in data:
    try:
        start_date = item.get("start_date")

        if not start_date:
            continue

        dt = datetime.strptime(start_date[:10], "%Y-%m-%d")

        if dt < cutoff_date:
            continue

        rows.append({
            "contract_amount": item.get("contract_amount"),
            "tenant_type": item.get("tenant_type"),
            "start_date": item.get("start_date"),
            "end_date": item.get("end_date")
        })

    except Exception as e:
        print("ROW ERROR:", e)

df = pd.DataFrame(rows)

df.drop_duplicates(inplace=True)

print("Rows collected:", len(df))

df.to_sql(
    "rent_contracts_90d",
    engine,
    if_exists="append",
    index=False
)

print(f"Inserted {len(df)} rows successfully")
