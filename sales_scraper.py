import pandas as pd
from sqlalchemy import create_engine
import os
import time

DATABASE_URL = os.getenv("DATABASE_URL")

CSV_FILE = "transactions_2026-04-29_07-47-39_1.csv"
TABLE_NAME = "dld_transactions_full"

engine = create_engine(DATABASE_URL)

print("Loading CSV...")
df = pd.read_csv(CSV_FILE)

print("Total rows:", len(df))

chunk_size = 5000
uploaded = 0
total_rows = len(df)

for start in range(0, total_rows, chunk_size):
    end = start + chunk_size

    chunk = df.iloc[start:end]

    try:
        chunk.to_sql(
            TABLE_NAME,
            engine,
            if_exists="append",
            index=False,
            method="multi"
        )

        uploaded += len(chunk)

        percent = (uploaded / total_rows) * 100

        print(f"Uploaded: {uploaded}/{total_rows} ({percent:.2f}%)")

    except Exception as e:
        print("ERROR:")
        print(e)

        print("Retrying in 15 seconds...")
        time.sleep(15)

print("DONE")
