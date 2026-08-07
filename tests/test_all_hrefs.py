import sys
import os
import json
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import codex_mgr

profiles = codex_mgr.get_profiles()
print("开始排查所有账号导航后的实际 URL 状态...")

import websocket
import urllib.request

for p in profiles:
    backup_dir = f"{codex_mgr.BACKUP_PREFIX}_{p}"
    temp_user_data = f"/tmp/codex_query_userdata_{p}"
    port = codex_mgr.find_free_port()
    
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
    proc = codex_mgr.subprocess.Popen(cmd, stdout=codex_mgr.subprocess.DEVNULL, stderr=codex_mgr.subprocess.DEVNULL)
    
    cdp_id = [0]
    def send_cdp(ws, method, params=None):
        cdp_id[0] += 1
        req_id = cdp_id[0]
        payload = {"id": req_id, "method": method}
        if params:
            payload["params"] = params
        ws.send(json.dumps(payload))
        while True:
            res_frame = json.loads(ws.recv())
            if res_frame.get("id") == req_id:
                return res_frame

    try:
        time.sleep(4.0)
        no_proxy_handler = urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(no_proxy_handler)
        
        list_url = f"http://127.0.0.1:{port}/json"
        req = urllib.request.Request(list_url, headers={"User-Agent": "curl/7.88.1"})
        ws_url = None
        with opener.open(req, timeout=5) as response:
            tabs = json.loads(response.read().decode('utf-8'))
            ws_url = tabs[0].get("webSocketDebuggerUrl")
            
        ws = websocket.create_connection(ws_url, timeout=15, skip_proxy=True)
        send_cdp(ws, "Runtime.enable")
        send_cdp(ws, "Page.bringToFront")
        
        ws.send(json.dumps({
            "id": 99999,
            "method": "Page.navigate",
            "params": {"url": "https://chatgpt.com/"}
        }))
        
        # 等待 8 秒让它充分跳转和加载
        time.sleep(8.0)
        
        res = send_cdp(ws, "Runtime.evaluate", {
            "expression": "window.location.href",
            "returnByValue": True
        })
        href = res.get("result", {}).get("result", {}).get("value")
        
        res_title = send_cdp(ws, "Runtime.evaluate", {
            "expression": "document.title",
            "returnByValue": True
        })
        title = res_title.get("result", {}).get("result", {}).get("value")
        
        print(f"账号: {p:<20} | 实际跳转 URL: {href:<60} | 页面标题: {title}")
        
        ws.close()
    except Exception as e:
        print(f"账号: {p:<20} | 发生错误: {e}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()
