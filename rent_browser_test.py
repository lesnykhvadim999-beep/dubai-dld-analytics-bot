import os
from playwright.sync_api import sync_playwright

os.system("playwright install chromium")

URL = "https://data.dubai/en/web/guest/l/468586"

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"]
    )

    page = browser.new_page()

    print("Opening page...")
    page.goto(URL, timeout=120000)

    print("PAGE TITLE:")
    print(page.title())

    print("SUCCESS")

    browser.close()
