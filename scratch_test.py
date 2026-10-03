import urllib.request
import urllib.parse
import json
import time

def test_auth():
    print("Starting test...")
    email = f"test_{int(time.time())}@company.com"
    password = "supersecretpassword123"
    
    def post(url, data, is_json=True):
        req = urllib.request.Request(f"http://localhost:8000{url}", method="POST")
        if is_json:
            req.add_header('Content-Type', 'application/json')
            encoded = json.dumps(data).encode('utf-8')
        else:
            req.add_header('Content-Type', 'application/x-www-form-urlencoded')
            encoded = urllib.parse.urlencode(data).encode('utf-8')
        
        try:
            with urllib.request.urlopen(req, data=encoded) as res:
                return res.status, json.loads(res.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())
            
    # 1. Signup
    print(f"Signing up {email}...")
    status, body = post("/auth/signup", {"email": email, "password": password, "company_name": "Test Company"})
    if status != 201:
        print("Signup failed:", status, body)
    else:
        print("Signup successful:", body)
        
    # 2. Login
    print("Logging in...")
    status, body = post("/auth/login", {"username": email, "password": password}, is_json=False)
    if status != 200:
        print("Login failed:", status, body)
    else:
        print("Login successful (access_token received):", "access_token" in body)
        
    # 3. Bad password
    print("Testing bad password...")
    status, body = post("/auth/login", {"username": email, "password": "wrongpassword"}, is_json=False)
    if status != 401:
        print("Expected 401, got:", status, body)
    else:
        print("Bad password handled correctly:", body)
        
    # 4. Duplicate signup
    print("Testing duplicate signup...")
    status, body = post("/auth/signup", {"email": email, "password": password, "company_name": "Test Company"})
    if status != 400:
        print("Expected 400, got:", status, body)
    else:
        print("Duplicate signup handled correctly:", body)

if __name__ == "__main__":
    test_auth()
