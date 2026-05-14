import os
import re
import json
import time
import glob
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

TABLE_NAME = "rent_residential"
PROGRESS_FILE = "rent_progress_residential.json"
CHUNK_SIZE = 10000

csv_files = glob.glob("rent_contracts*.csv")

if not csv_files:
    raise RuntimeError("CSV file not found")

FILE_NAME = max(csv_files, key=os.path.getsize)

print("Using CSV:", FILE_NAME)

IMPORTANT_COLUMNS = [
    "contract_amount",
    "property_type_en",
    "property_sub_type_en",
    "nearest_landmark_en",
    "nearest_metro_en",
    "nearest_mall_en",
    "area_en",
    "usage_en",
    "contract_start_date",
    "contract_end_date",
    "year"
]

def clean_column(name):
    name = str(name).strip().lower()
    name = re.sub(r"[^a-zA-Z0-9_]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r") as f:
            return json.load(f).get("last_done_row", 0)
    return 0

def save_progress(row_number):
    with open(PROGRESS_FILE, "w") as f:
        json.dump({"last_done_row": row_number}, f)

with open(FILE_NAME, "r", encoding="utf-8", errors="ignore") as f:
    total_rows = sum(1 for _ in f) - 1

print("Total rows:", total_rows)

last_done_row = load_progress()

reader = pd.read_csv(
    FILE_NAME,
    chunksize=CHUNK_SIZE,
    low_memory=False,
    on_bad_lines="skip"
)

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

table_created = False
current_row = 0

for chunk in reader:

    chunk_start = current_row
    chunk_end = current_row + len(chunk)
    current_row = chunk_end

    if chunk_end <= last_done_row:
        continue

    chunk.columns = [clean_column(c) for c in chunk.columns]

    available_cols = [c for c in IMPORTANT_COLUMNS if c in chunk.columns]

    chunk = chunk[available_cols]

    if "usage_en" in chunk.columns:
        chunk = chunk[
            chunk["usage_en"]
            .astype(str)
            .str.contains("Residential", case=False, na=False)
        ]

    if len(chunk) == 0:
        continue

    chunk.insert(
        0,
        "source_row_number",
        range(chunk_start + 1, chunk_start + 1 + len(chunk))
    )

    if not table_created:

        columns_sql = ", ".join([
            f'"{col}" TEXT'
            for col in chunk.columns
            if col != "source_row_number"
        ])

        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                source_row_number BIGINT PRIMARY KEY,
                {columns_sql}
            );
        """)

        conn.commit()
        table_created = True

    chunk = chunk.astype(str).replace({
        "nan": None,
        "NaT": None
    })

    columns = list(chunk.columns)

    values = [tuple(row) for row in chunk.to_numpy()]

    insert_sql = f"""
        INSERT INTO {TABLE_NAME}
        ({",".join([f'"{c}"' for c in columns])})
        VALUES %s
        ON CONFLICT (source_row_number) DO NOTHING;
    """

    while True:

        try:

            execute_values(
                cur,
                insert_sql,
                values,
                page_size=1000
            )

            conn.commit()

            save_progress(chunk_end)

            percent = chunk_end / total_rows * 100

            print(
                f"Uploaded: {chunk_end}/{total_rows} ({percent:.2f}%)"
            )

            break

        except Exception as e:

            conn.rollback()

            print("ERROR:")
            print(e)

            print("Retrying in 15 seconds...")

            time.sleep(15)

cur.close()
conn.close()

print("DONE")
