import urllib.request
import json
import ssl
import os

def test_api():
    auth_path = os.path.expanduser("~/.codex/auth.json")
    if not os.path.exists(auth_path):
        print("auth.json not found")
        return
        
    with open(auth_path, 'r') as f:
        auth_data = json.load(f)
        
    access_token = auth_data.get("tokens", {}).get("access_token")
    if not access_token:
        print("Access token not found in auth.json")
        return
        
    print("Sending request to check account status...")
    
    # We will fetch chatgpt.com/backend-api/accounts/check
    url = "https://chatgpt.com/backend-api/accounts/check"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
    )
    
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # Bypass SSL for testing to avoid proxy cert issues
    
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as response:
            res_data = response.read()
            print("Response Status:", response.status)
            parsed = json.loads(res_data.decode('utf-8'))
            print("Response Body (formatted):")
            print(json.dumps(parsed, indent=2))
    except Exception as e:
        print("API Request failed:", e)

if __name__ == "__main__":
    test_api()
