#!/usr/bin/env python3
"""
ChatGPT API 及 OpenAI Dashboard API 接口在线探针工具。
支持探测 accounts/check, models, usage, limits 以及 OpenAI Billing 订阅接口。
"""
import os
import sys
import json
import urllib.request
import urllib.error
import ssl
import subprocess

def probe_url(url, token, use_curl=False):
    print(f"\n=====================================")
    print(f"Probing: {url}")
    
    if use_curl:
        cmd = [
            "curl", "-s", "-i",
            "-H", f"Authorization: Bearer {token}",
            "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
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
        except Exception as e:
            print("Curl request failed:", e)
    else:
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            }
        )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=10) as response:
                print(f"Response Status: {response.status}")
                res_data = response.read()
                try:
                    parsed = json.loads(res_data.decode('utf-8'))
                    print("Body (JSON):")
                    print(json.dumps(parsed, indent=2))
                except Exception:
                    print("Body (Raw):", res_data[:300].decode('utf-8', errors='ignore'))
        except urllib.error.HTTPError as e:
            print(f"HTTP Error {e.code}: {e.reason}")
            try:
                body = e.read().decode('utf-8', errors='ignore')
                print("Error Body:", body[:300])
            except Exception:
                pass
        except Exception as e:
            print("Python urllib Request failed:", e)

def main():
    auth_path = os.path.expanduser("~/.codex/auth.json")
    if not os.path.exists(auth_path):
        print("auth.json not found in ~/.codex/auth.json")
        return
        
    with open(auth_path, 'r') as f:
        auth_data = json.load(f)
        
    access_token = auth_data.get("tokens", {}).get("access_token")
    account_id = auth_data.get("tokens", {}).get("account_id")
    
    if not access_token:
        print("No access token found in auth.json")
        return
        
    print(f"Found Access Token. Account ID: {account_id}")
    
    # 探针 Target 列表
    chatgpt_endpoints = [
        "https://chatgpt.com/backend-api/accounts/check",
        "https://chatgpt.com/backend-api/models",
    ]
    if account_id:
        chatgpt_endpoints.append(f"https://chatgpt.com/backend-api/accounts/{account_id}/usage")
        chatgpt_endpoints.append(f"https://chatgpt.com/backend-api/accounts/{account_id}/limits")
        
    openai_endpoints = [
        "https://api.openai.com/dashboard/billing/subscription",
        "https://api.openai.com/dashboard/billing/usage?start_date=2026-07-01&end_date=2026-07-31",
    ]
    
    print("\n--- Testing ChatGPT Backend APIs (urllib) ---")
    for url in chatgpt_endpoints:
        probe_url(url, access_token, use_curl=False)
        
    print("\n--- Testing OpenAI Dashboard APIs (curl) ---")
    for url in openai_endpoints:
        probe_url(url, access_token, use_curl=True)

if __name__ == "__main__":
    main()
