import os
import json
import subprocess

def probe_url(url, token):
    print(f"\n=====================================")
    print(f"Probing: {url}")
    cmd = [
        "curl", "-s", "-i",
        "-H", f"Authorization: Bearer {token}",
        "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        url
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        print("Status/Headers:")
        # Print first few lines of output (headers)
        lines = res.stdout.split('\r\n')
        for line in lines:
            if not line:
                break
            print(f"  {line}")
            
        # Try to print body
        body_idx = res.stdout.find('\r\n\r\n')
        if body_idx != -1:
            body = res.stdout[body_idx+4:]
            try:
                parsed = json.loads(body)
                print("Body (JSON):")
                print(json.dumps(parsed, indent=2))
            except Exception:
                print("Body (Raw):", body[:300])
        else:
            print("No body found or output was:", res.stdout[:300])
    except Exception as e:
        print("Failed to run curl:", e)

def main():
    auth_path = os.path.expanduser("~/.codex/auth.json")
    if not os.path.exists(auth_path):
        print("auth.json not found")
        return
        
    with open(auth_path, 'r') as f:
        auth_data = json.load(f)
        
    access_token = auth_data.get("tokens", {}).get("access_token")
    account_id = auth_data.get("tokens", {}).get("account_id")
    if not access_token:
        print("No access token found")
        return
        
    print(f"Account ID: {account_id}")
    
    # List of endpoints to probe
    endpoints = [
        "https://chatgpt.com/backend-api/accounts/check",
        "https://chatgpt.com/backend-api/models",
    ]
    if account_id:
        endpoints.append(f"https://chatgpt.com/backend-api/accounts/{account_id}/usage")
        endpoints.append(f"https://chatgpt.com/backend-api/accounts/{account_id}/limits")
        
    # We can also check if there is a specific usage or limit endpoint
    endpoints.append("https://chatgpt.com/backend-api/user_limit")

    for url in endpoints:
        probe_url(url, access_token)

if __name__ == "__main__":
    main()
