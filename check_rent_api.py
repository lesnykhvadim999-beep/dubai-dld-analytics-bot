import requests
import json

url = "https://data.dubai/o/dda/data-services/dataset-download?datasetId=468586&page=1&pageSize=30&sortDir=desc"

r = requests.get(url)

print(r.status_code)

data = r.json()

print(json.dumps(data, indent=2)[:5000])
