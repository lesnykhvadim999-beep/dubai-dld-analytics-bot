import os
import time
import pandas as pd
from sqlalchemy import create_engine, text

# =========================
# CONFIG
# =========================

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

CSV_FILE = "rent_contracts_2026-05-14_01-00-56_1.csv"

TABLE_NAME = "rent_residential_365d"

CHUNK_SIZE = 5000

# =========================
# CONNECT DB
# =========================

engine = create_engine(DATABASE_URL)

# =========================
# CREATE TABLE
# =========================

create_sql = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    contract_id TEXT,
    contract_date TEXT,
    property_type TEXT,
    property_sub_type TEXT,
    area TEXT,
    building TEXT,
    project TEXT,
    annual_amount DOUBLE PRECISION,
    contract_amount DOUBLE PRECISION,
    rooms TEXT,
    procedure_area DOUBLE PRECISION,
    registration_date TEXT
);
"""

with engine.begin() as conn:
    conn.execute(text(create_sql))

print("TABLE READY")

# =========================
# COUNT ROWS
# =========================

print("Counting rows...")

total_rows = sum(1 for _ in open(CSV_FILE, encoding="utf-8")) - 1

print(f"Total rows: {total_rows}")

# =========================
# LOAD CSV
# =========================

reader = pd.read_csv(
    CSV_FILE,
    chunksize=CHUNK_SIZE,
    low_memory=False
)

uploaded = 0

for chunk in reader:

    try:

        # =========================
        # FILTER RESIDENTIAL ONLY
        # =========================

        if "property_type_en" in chunk.columns:
            chunk = chunk[
                chunk["property_type_en"]
                .astype(str)
                .str.contains("Residential", case=False, na=False)
            ]

        # =========================
        # KEEP ONLY IMPORTANT COLUMNS
        # =========================

        columns_map = {
            "contract_id": "contract_id",
            "contract_date": "contract_date",
            "property_type_en": "property_type",
            "property_sub_type_en": "property_sub_type",
            "area_name_en": "area",
            "building_name_en": "building",
            "project_name_en": "project",
            "annual_amount": "annual_amount",
            "contract_amount": "contract_amount",
            "rooms_en": "rooms",
            "procedure_area": "procedure_area",
            "registration_date": "registration_date"
        }

        existing_cols = [
            col for col in columns_map.keys()
            if col in chunk.columns
        ]

        chunk = chunk[existing_cols]

        chunk.rename(columns=columns_map, inplace=True)

        # =========================
        # SAVE TO POSTGRES
        # =========================

        chunk.to_sql(
            TABLE_NAME,
            engine,
            if_exists="append",
            index=False,
            method="multi"
        )

        uploaded += len(chunk)

        percent = (uploaded / total_rows) * 100

        print(
            f"Uploaded: {uploaded}/{total_rows} ({percent:.2f}%)"
        )

    except Exception as e:

        print("ERROR:")
        print(e)

        print("Retrying in 15 seconds...")

        time.sleep(15)

print("DONE")
