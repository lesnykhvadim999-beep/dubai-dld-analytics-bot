import requests
import pandas as pd
from sqlalchemy import create_engine
from datetime import datetime, timedelta
import os

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

engine = create_engine(DATABASE_URL)

cutoff_date = datetime.now() - timedelta(days=365)

url = "https://www.dubaipulse.gov.ae/api/dataset/download/json"

params = {
    "dataset": "rental-contracts"
}

headers = {
    "User-Agent": "Mozilla/5.0"
}

print("Downloading CSV from Dubai Pulse...")

response = requests.get(
    url,
    params=params,
    headers=headers,
    timeout=120
)

print("Status:", response.status_code)

if response.status_code != 200:
    raise RuntimeError(f"Request failed: {response.status_code}")

with open("rent_data.csv", "wb") as f:
    f.write(response.content)

print("CSV downloaded")

df = pd.read_csv(
    "rent_data.csv",
    sep=";",
    engine="python",
    on_bad_lines="skip"
)

print("Rows in CSV:", len(df))

# Пробуем найти колонку с датой
date_column = None

for col in df.columns:
    if "date" in col.lower():
        date_column = col
        break

if date_column:
    df[date_column] = pd.to_datetime(
        df[date_column],
        errors="coerce"
    )

    df = df[df[date_column] >= cutoff_date]

print("Rows after filter:", len(df))

df.to_sql(
    "rent_contracts_90d",
    engine,
    if_exists="replace",
    index=False
)

print("DONE")
