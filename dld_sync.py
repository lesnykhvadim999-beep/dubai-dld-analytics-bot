import os
import re
import csv
import json
import time
from io import StringIO
from typing import Any, Dict, Iterable, List, Optional

import psycopg2
import requests

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

# IMPORTANT:
# public.dld_transactions_full contains Sales / Mortgages / Gifts only.
# Rentals must be loaded into public.dld_rents and main.py must read rentals from this table.

RENT_TABLE = "public.dld_rents"

# Dubai Pulse / DLD rental contracts API candidates.
# The old URL download.data.gov.ae is no longer reliable in Railway DNS.
RENT_API_URLS = [
    "https://api.dubaipulse.gov.ae/open/dld/dld_rent_contracts-open-api",
    "https://api.dubaipulse.gov.ae/shared/dld/dld_rent_contracts-open-api",
    "https://api.dubaipulse.gov.ae/open/dld/dld_rent_contracts-open",
    "https://api.dubaipulse.gov.ae/shared/dld/dld_rent_contracts-open",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Dubai-DLD-Analytics-Bot/1.0)",
    "Accept": "application/json,text/csv,text/plain,*/*",
}


def normalize_key(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_")


def first(row: Dict[str, Any], names: List[str]) -> Optional[Any]:
    if not row:
        return None
    lower = {normalize_key(k): v for k, v in row.items()}
    for name in names:
        key = normalize_key(name)
        if key in lower and lower[key] not in (None, ""):
            return lower[key]
    return None


def to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"[^0-9.\-]", "", text)
    if text in ("", ".", "-", "-."):
        return None
    try:
        return float(text)
    except Exception:
        return None


def extract_records(payload: Any) -> List[Dict[str, Any]]:
    """Accepts common Dubai Pulse API shapes and returns a list of records."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []

    for key in ("result", "data", "records", "items"):
        obj = payload.get(key)
        if isinstance(obj, list):
            return [x for x in obj if isinstance(x, dict)]
        if isinstance(obj, dict):
            for nested in ("records", "data", "items", "result"):
                arr = obj.get(nested)
                if isinstance(arr, list):
                    return [x for x in arr if isinstance(x, dict)]
    return []


def fetch_json_page(url: str, limit: int, offset: int) -> List[Dict[str, Any]]:
    params_variants = [
        {"limit": limit, "offset": offset},
        {"$limit": limit, "$offset": offset},
        {"pageSize": limit, "page": offset // limit + 1},
        {"size": limit, "page": offset // limit + 1},
    ]
    last_error = None
    for params in params_variants:
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=90)
            if r.status_code >= 400:
                last_error = f"HTTP {r.status_code}: {r.text[:200]}"
                continue
            ctype = r.headers.get("content-type", "").lower()
            text = r.text.strip()
            if not text:
                continue
            if "json" in ctype or text.startswith("{") or text.startswith("["):
                return extract_records(r.json())
            if "," in text and "\n" in text:
                return list(csv.DictReader(StringIO(text)))
        except Exception as exc:
            last_error = repr(exc)
            continue
    if last_error:
        print(f"Failed URL {url}: {last_error}")
    return []


def fetch_all_rent_records(max_rows: int = 300000, page_size: int = 5000) -> List[Dict[str, Any]]:
    for url in RENT_API_URLS:
        print(f"Trying rent source: {url}")
        all_rows: List[Dict[str, Any]] = []
        offset = 0
        empty_pages = 0
        while len(all_rows) < max_rows:
            rows = fetch_json_page(url, page_size, offset)
            if not rows:
                empty_pages += 1
                if empty_pages >= 2:
                    break
                offset += page_size
                continue
            empty_pages = 0
            all_rows.extend(rows)
            print(f"  loaded page offset={offset}, total={len(all_rows)}")
            if len(rows) < min(100, page_size):
                break
            offset += page_size
            time.sleep(0.2)
        if all_rows:
            print(f"Using source {url}; total rows fetched: {len(all_rows)}")
            return all_rows
    return []


def ensure_table(cur) -> None:
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {RENT_TABLE} (
            id SERIAL PRIMARY KEY,
            contract_number TEXT,
            contract_start_date TEXT,
            contract_end_date TEXT,
            area TEXT,
            project TEXT,
            property_type TEXT,
            property_sub_type TEXT,
            rooms TEXT,
            usage TEXT,
            contract_amount NUMERIC,
            annual_amount NUMERIC,
            property_size_sqm NUMERIC
        );
    """)
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_dld_rents_area ON {RENT_TABLE} (LOWER(area));")
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_dld_rents_project ON {RENT_TABLE} (LOWER(project));")
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_dld_rents_rooms ON {RENT_TABLE} (LOWER(rooms));")


def mapped_rent(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "contract_number": first(row, ["contract_number", "contract no", "ejari_contract_number", "contract_number_en"]),
        "contract_start_date": first(row, ["contract_start_date", "start_date", "contract start date", "start date"]),
        "contract_end_date": first(row, ["contract_end_date", "end_date", "contract end date", "end date"]),
        "area": first(row, ["area", "area_name_en", "area_name", "area en"]),
        "project": first(row, ["project", "project_name_en", "project_name", "building_name_en", "building_name", "property_name", "master_project", "master_project_en"]),
        "property_type": first(row, ["property_type", "property_type_en", "property type"]),
        "property_sub_type": first(row, ["property_sub_type", "property_sub_type_en", "property sub type", "unit_type"]),
        "rooms": first(row, ["number_of_rooms", "number of rooms", "rooms", "rooms_en", "room_type", "room type"]),
        "usage": first(row, ["usage", "usage_en", "property_usage", "property_usage_en"]),
        "contract_amount": to_float(first(row, ["contract_amount", "contract amount", "contract_value", "amount"])),
        "annual_amount": to_float(first(row, ["annual_amount", "annual amount", "annual_rent", "rent_value", "rent_amount"])),
        "property_size_sqm": to_float(first(row, ["property_size_sqm", "property_size", "property size", "property size sqm", "property_size_sq_m"])),
    }


def main() -> None:
    # Cron heartbeat (best-effort; never blocks sync)
    try:
        from auto_audit._common import record_metric
        record_metric("cron.tick.dld_sync", 1.0, meta={"status": "ok"})
    except Exception:
        pass
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    ensure_table(cur)
    conn.commit()

    rows = fetch_all_rent_records()
    if not rows:
        raise RuntimeError(
            "Could not fetch rent records from Dubai Pulse/DLD sources. "
            "Do not use this sync as Start Command permanently. Return Start Command to: python main.py"
        )

    print("Clearing old rent rows...")
    cur.execute(f"TRUNCATE TABLE {RENT_TABLE} RESTART IDENTITY;")

    inserted = 0
    for raw in rows:
        r = mapped_rent(raw)
        # Skip completely empty rows
        if not any(v not in (None, "") for v in r.values()):
            continue
        cur.execute(f"""
            INSERT INTO {RENT_TABLE} (
                contract_number,
                contract_start_date,
                contract_end_date,
                area,
                project,
                property_type,
                property_sub_type,
                rooms,
                usage,
                contract_amount,
                annual_amount,
                property_size_sqm
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            r["contract_number"],
            r["contract_start_date"],
            r["contract_end_date"],
            r["area"],
            r["project"],
            r["property_type"],
            r["property_sub_type"],
            r["rooms"],
            r["usage"],
            r["contract_amount"],
            r["annual_amount"],
            r["property_size_sqm"],
        ))
        inserted += 1
        if inserted % 5000 == 0:
            conn.commit()
            print(f"Inserted rent rows: {inserted}")

    conn.commit()
    cur.execute(f"SELECT COUNT(*) FROM {RENT_TABLE};")
    count = cur.fetchone()[0]
    print(f"DONE. dld_rents rows: {count}")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
