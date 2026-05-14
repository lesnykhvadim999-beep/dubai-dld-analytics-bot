from playwright.sync_api import sync_playwright
import pandas as pd
from sqlalchemy import create_engine
from datetime import datetime, timedelta
import os
import time

DATABASE_URL = os.getenv("DATABASE_URL")

engine = create_engine(DATABASE_URL)

cutoff_date = datetime.now() - timedelta(days=90)

rows = []

with sync_playwright() as p:

    browser = p.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu"
        ]
    )

    page = browser.new_page()

    page.goto(
        "https://data.dubai/en/web/guest/468586",
        wait_until="networkidle"
    )

    page.click("text=Data Table")

    time.sleep(8)

    for _ in range(50):

        table_rows = page.locator("tbody tr")
        count = table_rows.count()

        for i in range(count):

            cols = table_rows.nth(i).locator("td")

            try:

                contract_amount = cols.nth(0).inner_text().strip()
                tenant_type = cols.nth(4).inner_text().strip()
                start_date = cols.nth(5).inner_text().strip()
                end_date = cols.nth(6).inner_text().strip()

                dt = datetime.strptime(start_date, "%Y-%m-%d")

                if dt < cutoff_date:
                    continue

                rows.append({
                    "contract_amount": contract_amount,
                    "tenant_type": tenant_type,
                    "start_date": start_date,
                    "end_date": end_date
                })

            except Exception:
                pass

        page.mouse.wheel(0, 6000)

        time.sleep(2)

    browser.close()

df = pd.DataFrame(rows)

df.drop_duplicates(inplace=True)

df.to_sql(
    "rent_contracts_90d",
    engine,
    if_exists="append",
    index=False
)

print(f"Inserted {len(df)} rows")
