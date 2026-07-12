import urllib.request
import json
import subprocess
import time
from websocket import create_connection

def main():
    profile_name = "mytest"
    temp_user_data = f"/tmp/codex_query_userdata_{profile_name}"
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
        time.sleep(4.0)
        list_url = f"http://127.0.0.1:{port}/json"
        req = urllib.request.Request(list_url, headers={"User-Agent": "curl/7.88.1"})
        with urllib.request.urlopen(req, timeout=5) as response:
            tabs = json.loads(response.read().decode('utf-8'))
            
        ws_url = tabs[0].get("webSocketDebuggerUrl")
        ws = create_connection(ws_url, timeout=5)
        
        # 启用 Network 领域
        ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
        ws.recv()
        
        # 导航到 chatgpt 主页，触发网络请求
        print("Navigating to chatgpt.com to monitor requests...")
        ws.send(json.dumps({
            "id": 2,
            "method": "Page.navigate",
            "params": {"url": "https://chatgpt.com/"}
        }))
        ws.recv()
        
        # 监听 15 秒内的所有网络请求事件
        start_time = time.time()
        ws.settimeout(1.0)
        
        print("\nMonitoring HTTP traffic (Looking for backend-api)...")
        while time.time() - start_time < 15:
            try:
                frame = json.loads(ws.recv())
                method = frame.get("method")
                params = frame.get("params", {})
                
                # 如果是发送请求
                if method == "Network.requestWillBeSent":
                    request = params.get("request", {})
                    url = request.get("url", "")
                    if "backend-api" in url:
                        print(f"[Request] {request.get('method')}: {url}")
                        
                # 如果是收到响应
                elif method == "Network.responseReceived":
                    response = params.get("response", {})
                    url = response.get("url", "")
                    if "backend-api" in url:
                        print(f"[Response] Status {response.get('status')}: {url}")
                        # 尝试获取响应体
                        req_id = params.get("requestId")
                        body_payload = {
                            "id": 100 + int(time.time() * 100) % 1000,
                            "method": "Network.getResponseBody",
                            "params": {"requestId": req_id}
                        }
                        ws.send(json.dumps(body_payload))
                        # 读取直到拿到 getResponseBody 的返回
                        while True:
                            body_res = json.loads(ws.recv())
                            if body_res.get("id") == body_payload["id"]:
                                break
                        body_data = body_res.get("result", {}).get("body", "")
                        print(f"  └─ Body (trimmed): {body_data[:400]}")
                        
            except Exception:
                # 主要是 timeout
                pass
                
        ws.close()
    except Exception as e:
        print("Error:", e)
    finally:
        proc.terminate()
        proc.wait()

if __name__ == "__main__":
    main()
