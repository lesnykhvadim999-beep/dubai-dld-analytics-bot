import requests
import json

API_URL = "https://data.dubai/o/dda/data-services/dataset-download?datasetId=468586&page=1&pageSize=30&sortDir=desc"

headers = {
    "User-Agent": "Mozilla/5.0"
}

print("Checking Rent Contracts API...")

response = requests.get(API_URL, headers=headers, timeout=60)

print("Status:", response.status_code)
print("Content-Type:", response.headers.get("content-type"))
print("First 2000 chars:")
print(response.text[:2000])
