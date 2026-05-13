from playwright.sync_api import sync_playwright

URL = "https://data.dubai/en/web/guest/l/468586"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

    page = browser.new_page()

    print("Opening page...")
    page.goto(URL, timeout=120000)

    print("Page loaded")
    print(page.title())

    print(page.content()[:1000])

    browser.close()
