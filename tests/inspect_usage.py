import urllib.request
import json
import subprocess
import time
import os
from websocket import create_connection

def send_cdp_cmd(ws, method, params=None, req_id=1):
    payload = {"id": req_id, "method": method}
    if params:
        payload["params"] = params
    ws.send(json.dumps(payload))
    while True:
        res = json.loads(ws.recv())
        if res.get("id") == req_id:
            return res

def main():
    profile_name = "Julie_Perry"
    backup_dir = os.path.expanduser(f"~/Library/Application Support/Codex_Profile_{profile_name}")
    auth_path = os.path.join(backup_dir, "auth.json")
    
    if not os.path.exists(auth_path):
        print(f"Backup auth.json not found")
        return
        
    with open(auth_path, 'r') as f:
        auth_data = json.load(f)
        
    access_token = auth_data.get("tokens", {}).get("access_token")
    if not access_token:
        print("No access token found")
        return
        
    # Start headless ChatGPT
    temp_user_data = f"/tmp/codex_query_userdata_{profile_name}"
    if os.path.exists(temp_user_data):
        import shutil
        shutil.rmtree(temp_user_data)
    os.makedirs(os.path.join(temp_user_data, "Default"), exist_ok=True)
    
    app_support_backup = os.path.join(backup_dir, "app_support", "Default")
    if os.path.exists(app_support_backup):
        import shutil
        for item in ["Cookies", "Cookies-journal"]:
            src = os.path.join(app_support_backup, item)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(temp_user_data, "Default", item))
        for folder in ["Local Storage", "Session Storage"]:
            src_folder = os.path.join(app_support_backup, folder)
            if os.path.exists(src_folder):
                shutil.copytree(src_folder, os.path.join(temp_user_data, "Default", folder))

    port = 9299
    cmd = [
        "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
        f"--user-data-dir={temp_user_data}",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--disable-blink-features=AutomationControlled",
        "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 ChatGPT/1.0.0",
        "--blink-settings=imagesEnabled=false",
        "--headless"
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    try:
        tabs = None
        for _ in range(8):
            time.sleep(1.5)
            try:
                list_url = f"http://127.0.0.1:{port}/json"
                req = urllib.request.Request(list_url, headers={"User-Agent": "curl/7.88.1"})
                with urllib.request.urlopen(req, timeout=3) as response:
                    tabs = json.loads(response.read().decode('utf-8'))
                    if tabs:
                        break
            except Exception:
                pass
                
        if not tabs:
            print("Failed to launch headless browser tabs")
            return
            
        ws_url = tabs[0].get("webSocketDebuggerUrl")
        ws = create_connection(ws_url, timeout=10)
        
        send_cdp_cmd(ws, "Runtime.enable", req_id=1)
        
        ws.send(json.dumps({
            "id": 2,
            "method": "Page.navigate",
            "params": {"url": "https://chatgpt.com/"}
        }))
        time.sleep(4.5)
        
        # Test targets
        targets = [
            "https://chatgpt.com/backend-api/codex/models?client_version=0.144.0",
            "https://chatgpt.com/backend-api/codex/models",
            "https://chatgpt.com/backend-api/codex/usage",
            "https://chatgpt.com/backend-api/codex/settings/usage",
            "https://chatgpt.com/backend-api/codex/settings"
        ]
        
        for idx, url in enumerate(targets):
            print(f"\nTesting: {url}")
            # 清空全局变量
            send_cdp_cmd(ws, "Runtime.evaluate", {"expression": "window.__my_res = null;"}, req_id=100 + idx*10)
            
            # 发起异步 fetch 写入全局变量，防止挂起
            js = f"""
            fetch('{url}', {{
                headers: {{ 'Authorization': 'Bearer {access_token}' }}
            }}).then(r => r.json().catch(() => r.text())).then(data => {{
                window.__my_res = data;
            }}).catch(e => {{
                window.__my_res = {{error: e.message}};
            }})
            """
            send_cdp_cmd(ws, "Runtime.evaluate", {"expression": js}, req_id=101 + idx*10)
            
            # 轮询 12 次 (最大等待 6 秒) 获取结果
            res_val = None
            for _ in range(12):
                time.sleep(0.5)
                res = send_cdp_cmd(ws, "Runtime.evaluate", {
                    "expression": "window.__my_res",
                    "returnByValue": True
                }, req_id=102 + idx*10 + _)
                val = res.get("result", {}).get("result", {}).get("value")
                if val is not None:
                    res_val = val
                    break
            
            if res_val is None:
                print("Result: Timeout (no response in 6 seconds)")
            else:
                print("Result:")
                if isinstance(res_val, dict) or isinstance(res_val, list):
                    print(json.dumps(res_val, indent=2))
                else:
                    print(str(res_val)[:600])
                    
        ws.close()
    except Exception as e:
        print("Error:", e)
    finally:
        proc.terminate()
        proc.wait()

if __name__ == "__main__":
    main()
