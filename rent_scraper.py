import requests
import pandas as pd
from sqlalchemy import create_engine
from datetime import datetime, timedelta
import os

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

engine = create_engine(DATABASE_URL)

url = "https://www.dubaipulse.gov.ae/api/dataset/download/download_attachment"

params = {
    "dataset": "rental-contracts",
    "format": "csv"
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

df = pd.read_csv("rent_data.csv")

print("Rows in CSV:", len(df))

df.to_sql(
    "rent_contracts_90d",
    engine,
    if_exists="replace",
    index=False
)

print("DONE")
