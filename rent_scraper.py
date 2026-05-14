import pandas as pd
from sqlalchemy import create_engine
import os

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

engine = create_engine(DATABASE_URL)

FILE_NAME = "rent_contracts_2026-05-14_01-00-56_1.csv"

print("Reading CSV...")

df = pd.read_csv(
    FILE_NAME,
    low_memory=False
)

print("Rows in CSV:", len(df))

print("Uploading to PostgreSQL...")

df.to_sql(
    "rent_contracts",
    engine,
    if_exists="replace",
    index=False,
    chunksize=5000,
    method="multi"
)

print("DONE")
