import requests
import pandas as pd
from sqlalchemy import create_engine
import os

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

engine = create_engine(DATABASE_URL)

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

text = response.text

if "<!DOCTYPE html>" in text:
    print("Dubai Pulse returned HTML instead of JSON")
    print(text[:500])
    exit()

data = response.json()

rows = []

for item in data[:5000]:
    try:
        rows.append({
            "contract_amount": item.get("contract_amount"),
            "tenant_type": item.get("tenant_type"),
            "start_date": item.get("start_date"),
            "end_date": item.get("end_date")
        })
    except Exception as e:
        print("ROW ERROR:", e)

df = pd.DataFrame(rows)

print("Rows:", len(df))

df.to_sql(
    "rent_contracts_90d",
    engine,
    if_exists="replace",
    index=False
)

print("DONE")
