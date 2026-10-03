import urllib.request, json
req = urllib.request.Request("http://127.0.0.1:8000/auth/signup", data=json.dumps({"email": "amazon_test@company.com", "password": "password", "company_name": "Test"}).encode(), headers={"Content-Type": "application/json"})
res = urllib.request.urlopen(req)
req = urllib.request.Request("http://127.0.0.1:8000/auth/login", data="username=amazon_test%40company.com&password=password".encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
token = json.loads(urllib.request.urlopen(req).read().decode())["access_token"]
req = urllib.request.Request("http://127.0.0.1:8000/ads-accounts/connect", data=b'', headers={"Authorization": f"Bearer {token}"})
print(urllib.request.urlopen(req).read().decode())
