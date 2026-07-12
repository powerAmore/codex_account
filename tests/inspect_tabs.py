import urllib.request
import json
import subprocess
import time
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
    temp_user_data = "/tmp/codex_query_userdata_mytest"
    port = 9299
    
    # Enhanced parameters to bypass Cloudflare in headless mode
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
        
        # Enable Runtime & Page
        send_cdp_cmd(ws, "Runtime.enable", req_id=1)
        send_cdp_cmd(ws, "Page.enable", req_id=2)
        
        # Navigate to chatgpt.com
        print("Navigating to chatgpt.com...")
        send_cdp_cmd(ws, "Page.navigate", {"url": "https://chatgpt.com/"}, req_id=3)
        
        # Wait for load
        print("Waiting for page load...")
        time.sleep(6.0)
        
        # Print current location
        res_loc = send_cdp_cmd(ws, "Runtime.evaluate", {
            "expression": "window.location.href",
            "returnByValue": True
        }, req_id=4)
        print("Location after nav:", res_loc.get("result", {}).get("result", {}).get("value"))
        
        # Fetch backend-api
        fetch_js = "fetch('https://chatgpt.com/backend-api/models').then(r => r.text()).catch(e => 'FETCH_ERROR: ' + e.message)"
        res_fetch = send_cdp_cmd(ws, "Runtime.evaluate", {
            "expression": fetch_js,
            "awaitPromise": True,
            "returnByValue": True
        }, req_id=5)
        
        val = res_fetch.get("result", {}).get("result", {}).get("value", "")
        print("\nFetch Result (trimmed):")
        print(val[:800])
        
        ws.close()
    except Exception as e:
        print("Error:", e)
    finally:
        proc.terminate()
        proc.wait()

if __name__ == "__main__":
    main()
