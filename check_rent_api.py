import requests

url = "https://data.dubai/o/dda/data-services/dataset-download?datasetId=468586&page=1&pageSize=30&sortDir=desc"

headers = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "*/*"
}

r = requests.get(url, headers=headers, timeout=60)

print("STATUS:", r.status_code)
print("CONTENT TYPE:", r.headers.get("content-type"))
print("TEXT:")
print(r.text[:5000])
