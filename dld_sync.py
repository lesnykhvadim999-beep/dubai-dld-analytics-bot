import os
import psycopg2
import requests
import csv
from io import StringIO

DATABASE_URL = os.getenv("DATABASE_URL")

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS dld_transactions (
    id SERIAL PRIMARY KEY,
    transaction_date TEXT,
    project_name TEXT,
    area_name TEXT,
    property_type TEXT,
    procedure_area TEXT,
    actual_worth REAL
)
""")

conn.commit()

print("Downloading DLD open data...")

url = "https://download.data.gov.ae/dataset/5b64f6d4-0ec0-40db-88d1-2e5c9b87222b/resource/24e3014e-44f1-44db-b7c0-8d4e7edc8db1/download/transactions.csv"

response = requests.get(url)

csv_data = StringIO(response.text)

reader = csv.DictReader(csv_data)

inserted = 0

for row in reader:
    try:
        cur.execute("""
            INSERT INTO dld_transactions (
                transaction_date,
                project_name,
                area_name,
                property_type,
                procedure_area,
                actual_worth
            )
            VALUES (%s,%s,%s,%s,%s,%s)
        """, (
            row.get("instance_date"),
            row.get("project_name_en"),
            row.get("area_name_en"),
            row.get("property_type_en"),
            row.get("procedure_area"),
            row.get("actual_worth")
        ))

        inserted += 1

    except Exception as e:
        print(e)

conn.commit()

print(f"Inserted: {inserted} rows")
