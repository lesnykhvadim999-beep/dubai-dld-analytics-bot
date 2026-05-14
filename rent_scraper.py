import os
import time
import pandas as pd
from sqlalchemy import create_engine

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

FILE_NAME = "rent_contracts_2026-05-14_01-00-56_1.csv"
TABLE_NAME = "rent_contracts"

engine = create_engine(DATABASE_URL)

print("Counting rows...")
with open(FILE_NAME, "r", encoding="utf-8", errors="ignore") as f:
    total_rows = sum(1 for _ in f) - 1

print(f"Total rows: {total_rows}")

# 30% этапами
stage_size = max(int(total_rows * 0.30), 1)

# внутри этапа грузим маленькими пачками, чтобы не падало
inner_chunk_size = 5000

uploaded_rows = 0
first_chunk = True
stage_number = 1

while uploaded_rows < total_rows:
    stage_end = min(uploaded_rows + stage_size, total_rows)

    print(f"\n=== STAGE {stage_number}: {uploaded_rows} -> {stage_end} rows ===")

    rows_in_stage = 0

    reader = pd.read_csv(
        FILE_NAME,
        skiprows=range(1, uploaded_rows + 1),
        nrows=stage_end - uploaded_rows,
        chunksize=inner_chunk_size,
        low_memory=False,
        on_bad_lines="skip"
    )

    for chunk in reader:
        while True:
            try:
                chunk.to_sql(
                    TABLE_NAME,
                    engine,
                    if_exists="replace" if first_chunk else "append",
                    index=False,
                    chunksize=1000,
                    method="multi"
                )

                first_chunk = False

                rows_uploaded_now = len(chunk)
                uploaded_rows += rows_uploaded_now
                rows_in_stage += rows_uploaded_now

                percent = uploaded_rows / total_rows * 100

                print(
                    f"Uploaded: {uploaded_rows}/{total_rows} "
                    f"({percent:.2f}%)"
                )

                break

            except Exception as e:
                print("ERROR during upload:")
                print(e)
                print("Retrying in 10 seconds...")
                time.sleep(10)

    print(f"Stage {stage_number} completed.")
    stage_number += 1

print("\nDONE. Full CSV uploaded successfully.")
