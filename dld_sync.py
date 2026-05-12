import os
import io
import pandas as pd
import requests
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
DLD_TRANSACTIONS_URL = os.getenv("DLD_TRANSACTIONS_URL")
DLD_RENTS_URL = os.getenv("DLD_RENTS_URL")


def get_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing")
    return psycopg2.connect(DATABASE_URL)


def create_tables():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS dld_transactions (
        id SERIAL PRIMARY KEY,
        transaction_number TEXT UNIQUE,
        transaction_date TEXT,
        transaction_type TEXT,
        registration_type TEXT,
        area TEXT,
        property_type TEXT,
        property_sub_type TEXT,
        amount NUMERIC,
        property_size_sqm NUMERIC,
        rooms TEXT,
        project TEXT,
        master_project TEXT,
        raw_json JSONB
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS dld_rents (
        id SERIAL PRIMARY KEY,
        contract_number TEXT UNIQUE,
        contract_start_date TEXT,
        contract_end_date TEXT,
        area TEXT,
        property_type TEXT,
        property_sub_type TEXT,
        annual_amount NUMERIC,
        property_size_sqm NUMERIC,
        rooms TEXT,
        project TEXT,
        raw_json JSONB
    );
    """)

    conn.commit()
    cur.close()
    conn.close()


def download_csv(url):
    if not url:
        raise RuntimeError("DLD URL is missing")

    response = requests.get(url, timeout=120)
    response.raise_for_status()

    return pd.read_csv(io.StringIO(response.text))


def normalize_column(name):
    return (
        str(name)
        .strip()
        .lower()
        .replace(" ", "_")
        .replace("-", "_")
        .replace("(", "")
        .replace(")", "")
        .replace("/", "_")
    )


def clean_dataframe(df):
    df.columns = [normalize_column(c) for c in df.columns]
    return df


def safe_get(row, *names):
    for name in names:
        if name in row and pd.notna(row[name]):
            return row[name]
    return None


def to_number(value):
    if value is None or pd.isna(value):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except Exception:
        return None


def sync_transactions():
    df = clean_dataframe(download_csv(DLD_TRANSACTIONS_URL))

    conn = get_connection()
    cur = conn.cursor()

    inserted = 0

    for _, row in df.iterrows():
        raw = row.to_dict()

        transaction_number = safe_get(row, "transaction_number", "transaction_no", "procedure_number")
        transaction_date = safe_get(row, "transaction_date", "procedure_date")
        transaction_type = safe_get(row, "transaction_type", "procedure_name")
        registration_type = safe_get(row, "registration_type")
        area = safe_get(row, "area", "area_name_en", "area_name")
        property_type = safe_get(row, "property_type", "property_usage")
        property_sub_type = safe_get(row, "property_sub_type", "property_sub_type_en")
        amount = to_number(safe_get(row, "amount", "actual_worth", "procedure_value"))
        property_size_sqm = to_number(safe_get(row, "property_size_sqm", "property_size", "actual_area"))
        rooms = safe_get(row, "rooms", "rooms_en", "room")
        project = safe_get(row, "project", "project_name", "project_name_en")
        master_project = safe_get(row, "master_project", "master_project_en")

        if not transaction_number:
            continue

        cur.execute("""
        INSERT INTO dld_transactions (
            transaction_number,
            transaction_date,
            transaction_type,
            registration_type,
            area,
            property_type,
            property_sub_type,
            amount,
            property_size_sqm,
            rooms,
            project,
            master_project,
            raw_json
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (transaction_number) DO NOTHING;
        """, (
            str(transaction_number),
            str(transaction_date) if transaction_date is not None else None,
            str(transaction_type) if transaction_type is not None else None,
            str(registration_type) if registration_type is not None else None,
            str(area) if area is not None else None,
            str(property_type) if property_type is not None else None,
            str(property_sub_type) if property_sub_type is not None else None,
            amount,
            property_size_sqm,
            str(rooms) if rooms is not None else None,
            str(project) if project is not None else None,
            str(master_project) if master_project is not None else None,
            pd.Series(raw).to_json()
        ))

        inserted += cur.rowcount

    conn.commit()
    cur.close()
    conn.close()

    return inserted


def sync_rents():
    if not DLD_RENTS_URL:
        return 0

    df = clean_dataframe(download_csv(DLD_RENTS_URL))

    conn = get_connection()
    cur = conn.cursor()

    inserted = 0

    for _, row in df.iterrows():
        raw = row.to_dict()

        contract_number = safe_get(row, "contract_number", "contract_no", "ejari_contract_number")
        contract_start_date = safe_get(row, "contract_start_date", "start_date")
        contract_end_date = safe_get(row, "contract_end_date", "end_date")
        area = safe_get(row, "area", "area_name_en", "area_name")
        property_type = safe_get(row, "property_type", "property_usage")
        property_sub_type = safe_get(row, "property_sub_type", "property_sub_type_en")
        annual_amount = to_number(safe_get(row, "annual_amount", "annual_rent", "rent_value"))
        property_size_sqm = to_number(safe_get(row, "property_size_sqm", "property_size", "actual_area"))
        rooms = safe_get(row, "rooms", "rooms_en", "room")
        project = safe_get(row, "project", "project_name", "project_name_en")

        if not contract_number:
            continue

        cur.execute("""
        INSERT INTO dld_rents (
            contract_number,
            contract_start_date,
            contract_end_date,
            area,
            property_type,
            property_sub_type,
            annual_amount,
            property_size_sqm,
            rooms,
            project,
            raw_json
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (contract_number) DO NOTHING;
        """, (
            str(contract_number),
            str(contract_start_date) if contract_start_date is not None else None,
            str(contract_end_date) if contract_end_date is not None else None,
            str(area) if area is not None else None,
            str(property_type) if property_type is not None else None,
            str(property_sub_type) if property_sub_type is not None else None,
            annual_amount,
            property_size_sqm,
            str(rooms) if rooms is not None else None,
            str(project) if project is not None else None,
            pd.Series(raw).to_json()
        ))

        inserted += cur.rowcount

    conn.commit()
    cur.close()
    conn.close()

    return inserted


def run_sync():
    create_tables()
    tx_count = sync_transactions()
    rent_count = sync_rents()

    print(f"DLD sync completed. Transactions inserted: {tx_count}. Rents inserted: {rent_count}.")


if __name__ == "__main__":
    run_sync()
