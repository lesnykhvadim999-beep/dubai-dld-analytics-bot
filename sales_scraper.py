import os
import time
import pandas as pd
from sqlalchemy import create_engine

DATABASE_URL = os.getenv("DATABASE_URL")
CSV_FILE = os.getenv("CSV_FILE", "Real_Estate_Transactions_2026-05-15.csv")
TABLE_NAME = "dld_transactions_full"

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

engine = create_engine(DATABASE_URL)

print("Starting sales updater...")

if not os.path.exists(CSV_FILE):
    print(f"CSV file not found: {CSV_FILE}")
    print("Updater finished without changes.")
    exit(0)

print("Loading CSV:", CSV_FILE)

df = pd.read_csv(CSV_FILE)
print("Total rows in CSV:", len(df))

chunk_size = 5000
uploaded = 0
total_rows = len(df)

for start in range(0, total_rows, chunk_size):
    chunk = df.iloc[start:start + chunk_size]

    try:
        chunk.to_sql(
            TABLE_NAME,
            engine,
            if_exists="append",
            index=False,
            method="multi"
        )

        uploaded += len(chunk)
        percent = uploaded / total_rows * 100
        print(f"Uploaded: {uploaded}/{total_rows} ({percent:.2f}%)")

    except Exception as e:
        print("ERROR:")
        print(e)
        print("Retrying in 15 seconds...")
        time.sleep(15)

print("DONE")
