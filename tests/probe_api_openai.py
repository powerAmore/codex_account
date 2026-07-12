import os
import json
import subprocess

def probe_openai_api(url, token):
    print(f"\n=====================================")
    print(f"Probing: {url}")
    cmd = [
        "curl", "-s", "-i",
        "-H", f"Authorization: Bearer {token}",
        url
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        print("Status/Headers:")
        lines = res.stdout.split('\r\n')
        for line in lines:
            if not line:
                break
            print(f"  {line}")
            
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
            print("No body found")
    except Exception as e:
        print("Failed:", e)

def main():
    auth_path = os.path.expanduser("~/.codex/auth.json")
    if not os.path.exists(auth_path):
        print("auth.json not found")
        return
        
    with open(auth_path, 'r') as f:
        auth_data = json.load(f)
        
    access_token = auth_data.get("tokens", {}).get("access_token")
    if not access_token:
        print("No access token found")
        return
        
    # OpenAI billing/usage URLs
    urls = [
        "https://api.openai.com/dashboard/billing/subscription",
        "https://api.openai.com/dashboard/billing/usage?start_date=2026-07-01&end_date=2026-07-31",
        "https://api.openai.com/v1/dashboard/billing/subscription",
        "https://api.openai.com/v1/dashboard/billing/usage?start_date=2026-07-01&end_date=2026-07-31"
    ]
    
    for url in urls:
        probe_openai_api(url, access_token)

if __name__ == "__main__":
    main()
