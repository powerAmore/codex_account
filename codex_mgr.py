#!/usr/bin/env python3
import os
import sys
import json
import base64
import shutil
import subprocess
import time
import datetime
import urllib.request
import urllib.error

# 安装缺失的 websocket-client 依赖
try:
    import websocket
except ImportError:
    print("正在安装必要依赖 websocket-client...")
    try:
        subprocess.run(["uv", "pip", "install", "websocket-client"], check=True, capture_output=True)
    except Exception:
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "websocket-client"], check=True, capture_output=True)
        except Exception as e:
            print(f"安装依赖失败，请手动运行 'pip install websocket-client': {e}")
            sys.exit(1)
    import websocket

# 配置路径
APP_SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/Codex")
BACKUP_PREFIX = os.path.expanduser("~/Library/Application Support/Codex_Profile")
ACTIVE_FILE = os.path.expanduser("~/Library/Application Support/.active_codex_profile")
CODEX_HOME = os.path.expanduser("~/.codex")
AUTH_FILE = os.path.join(CODEX_HOME, "auth.json")
USAGE_CACHE_FILE = os.path.join(CODEX_HOME, "accounts_usage.json")

# 颜色控制
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
CYAN = '\033[96m'
BOLD = '\033[1m'
RESET = '\033[0m'

def get_current_active():
    if os.path.exists(ACTIVE_FILE):
        try:
            with open(ACTIVE_FILE, 'r') as f:
                return f.read().strip()
        except Exception:
            pass
    return None

def set_current_active(name):
    try:
        with open(ACTIVE_FILE, 'w') as f:
            f.write(name)
    except Exception as e:
        print(f"[{RED}ERROR{RESET}] 无法写入活跃配置文件: {e}")

def get_profiles():
    profiles = []
    parent = os.path.expanduser("~/Library/Application Support")
    if os.path.exists(parent):
        for item in os.listdir(parent):
            if item.startswith("Codex_Profile_") and os.path.isdir(os.path.join(parent, item)):
                profiles.append(item.replace("Codex_Profile_", ""))
    return sorted(profiles)

def decode_jwt_payload(token):
    try:
        parts = token.split('.')
        if len(parts) < 2:
            return None
        payload = parts[1]
        # Base64Url padding
        payload += '=' * (4 - len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return None

def kill_chatgpt_processes():
    print("正在安全关闭 ChatGPT / Codex 进程...")
    subprocess.run(["pkill", "-f", "ChatGPT"], capture_output=True)
    subprocess.run(["pkill", "-f", "Codex"], capture_output=True)
    time.sleep(1.5)
    
    # 双重检查
    res = subprocess.run(["pgrep", "-f", "ChatGPT.app"], capture_output=True)
    if res.returncode == 0:
        print(f"{YELLOW}提示: 检测到 ChatGPT 仍有残余进程，强行关闭中...{RESET}")
        subprocess.run(["pkill", "-9", "-f", "ChatGPT"], capture_output=True)
        subprocess.run(["pkill", "-9", "-f", "Codex"], capture_output=True)
        time.sleep(1.0)

def backup_profile(profile_name):
    backup_dir = f"{BACKUP_PREFIX}_{profile_name}"
    os.makedirs(backup_dir, exist_ok=True)
    
    # 备份 App Support 核心文件
    app_support_backup = os.path.join(backup_dir, "app_support", "Default")
    os.makedirs(app_support_backup, exist_ok=True)
    
    default_dir = os.path.join(APP_SUPPORT_DIR, "Default")
    if os.path.exists(default_dir):
        print(f"正在增量备份活跃账号 Cookies & 本地存储...")
        # 仅备份核心认证文件以极大地压缩体积
        for item in ["Cookies", "Cookies-journal"]:
            src = os.path.join(default_dir, item)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(app_support_backup, item))
                
        # 复制 Local Storage / Session Storage
        for folder in ["Local Storage", "Session Storage"]:
            src_folder = os.path.join(default_dir, folder)
            dst_folder = os.path.join(app_support_backup, folder)
            if os.path.exists(src_folder):
                if os.path.exists(dst_folder):
                    shutil.rmtree(dst_folder)
                shutil.copytree(src_folder, dst_folder)
    
    # 备份 auth.json
    if os.path.exists(AUTH_FILE):
        shutil.copy2(AUTH_FILE, os.path.join(backup_dir, "auth.json"))

def restore_profile(profile_name):
    backup_dir = f"{BACKUP_PREFIX}_{profile_name}"
    if not os.path.exists(backup_dir):
        print(f"[{RED}ERROR{RESET}] Profile {profile_name} 备份不存在。")
        return False
        
    print(f"正在加载账号 '{profile_name}' 的状态...")
    os.makedirs(APP_SUPPORT_DIR, exist_ok=True)
    default_dir = os.path.join(APP_SUPPORT_DIR, "Default")
    os.makedirs(default_dir, exist_ok=True)
    
    app_support_backup = os.path.join(backup_dir, "app_support", "Default")
    if os.path.exists(app_support_backup):
        # 还原核心认证文件
        for item in ["Cookies", "Cookies-journal"]:
            src = os.path.join(app_support_backup, item)
            dst = os.path.join(default_dir, item)
            if os.path.exists(src):
                shutil.copy2(src, dst)
            elif os.path.exists(dst):
                try:
                    os.remove(dst)
                except Exception:
                    pass
                    
        # 还原 Local Storage / Session Storage
        for folder in ["Local Storage", "Session Storage"]:
            src_folder = os.path.join(app_support_backup, folder)
            dst_folder = os.path.join(default_dir, folder)
            if os.path.exists(src_folder):
                if os.path.exists(dst_folder):
                    shutil.rmtree(dst_folder)
                shutil.copytree(src_folder, dst_folder)
                
    # 还原 auth.json
    backup_auth = os.path.join(backup_dir, "auth.json")
    if os.path.exists(backup_auth):
        os.makedirs(CODEX_HOME, exist_ok=True)
        shutil.copy2(backup_auth, AUTH_FILE)
    elif os.path.exists(AUTH_FILE):
        try:
            os.remove(AUTH_FILE)
        except Exception:
            pass
            
    set_current_active(profile_name)
    return True

def cmd_add(profile_name=None):
    # 1. 尝试从当前的 auth.json 自动提取姓名和邮箱
    detected_email = None
    detected_name = None
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE, "r") as f:
                auth_data = json.load(f)
                id_token = auth_data.get("tokens", {}).get("id_token")
                if id_token:
                    payload = decode_jwt_payload(id_token)
                    detected_email = payload.get("email")
                    detected_name = payload.get("name")
        except Exception:
            pass

    if not profile_name:
        if detected_name:
            # 过滤只允许 valid_chars 字符，其它非合规字符和空格一律替换为下划线
            valid_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.@")
            candidate_name = "".join(c if c in valid_chars else "_" for c in detected_name)
            while "__" in candidate_name:
                candidate_name = candidate_name.replace("__", "_")
            candidate_name = candidate_name.strip("_")
            
            # 校验别名唯一性
            backup_dir = f"{BACKUP_PREFIX}_{candidate_name}"
            if candidate_name and not os.path.exists(backup_dir):
                profile_name = candidate_name
                print(f"检测到当前登录的真实姓名: '{detected_name}'，自动命名为: '{profile_name}'")
                
        if not profile_name and detected_email:
            profile_name = detected_email
            print(f"检测到当前登录的邮箱: '{profile_name}'")
            
        if not profile_name:
            print(f"{RED}错误: 未检测到任何登录状态，且未指定 Profile 名称。{RESET}")
            print("请登录 ChatGPT App 客户端后再试，或者指定别名：")
            print("  示例: python3 codex_mgr.py add my_alias")
            return
            
    # 限制名称格式
    valid_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.@")
    if not all(c in valid_chars for c in profile_name):
        print(f"{RED}错误: Profile 名称只允许英文字母、数字、下划线、连字符、点和 @ 符号。{RESET}")
        return
        
    backup_dir = f"{BACKUP_PREFIX}_{profile_name}"
    if os.path.exists(backup_dir):
        print(f"{RED}错误: 账号 Profile '{profile_name}' 已经存在。{RESET}")
        return

    # 1. 退出进程以保证文件拷贝安全完整
    print(f"正在安全关闭 ChatGPT / Codex 进程...")
    kill_chatgpt_processes()
    
    # 2. 对当前可能活跃的旧 Profile 执行最新的增量备份
    current = get_current_active()
    if current:
        print(f"正在增量备份当前活跃账号 '{current}' 的最新状态...")
        backup_profile(current)
        
    # 3. 创建新 Profile 目录并备份当前数据
    print(f"正在将当前登录态保存为新 Profile: '{profile_name}'...")
    os.makedirs(backup_dir, exist_ok=True)
    backup_profile(profile_name)
    set_current_active(profile_name)
    print(f"{GREEN}账号 Profile '{profile_name}' 创建并备份成功！{RESET}")
    
    # 4. 重新拉起客户端
    print("正在重新打开 ChatGPT 应用程序...")
    subprocess.run(["open", "-a", "/Applications/ChatGPT.app"])

def cmd_switch(target_profile):
    # 1. 获取所有可用的 Profile 列表
    parent_dir = os.path.expanduser("~/Library/Application Support")
    prefix = os.path.basename(BACKUP_PREFIX)
    profiles = []
    if os.path.exists(parent_dir):
        for name in os.listdir(parent_dir):
            if name.startswith(prefix + "_"):
                profiles.append(name[len(prefix) + 1:])
                
    # 2. 匹配逻辑 (优先精确匹配，再进行模糊匹配)
    if target_profile in profiles:
        pass
    else:
        matched = []
        for p in profiles:
            if target_profile.lower() in p.lower():
                matched.append(p)
                
        if len(matched) == 1:
            resolved_profile = matched[0]
            print(f"智能匹配到唯一 Profile: '{resolved_profile}'")
            target_profile = resolved_profile
        elif len(matched) > 1:
            print(f"{RED}错误: 发现多个匹配的 Profile: {', '.join(matched)}{RESET}")
            print("请指定更精确的名称。")
            return

    current = get_current_active()
    if current == target_profile:
        print(f"您当前已处于账号 '{target_profile}' 下！正在重新打开 App...")
        subprocess.run(["open", "-a", "/Applications/ChatGPT.app"])
        return
        
    backup_dir = f"{BACKUP_PREFIX}_{target_profile}"
    if not os.path.exists(backup_dir):
        print(f"{RED}错误: 账号 Profile '{target_profile}' 不存在。{RESET}")
        print(f"提示: 请先运行 'python3 codex_mgr.py add' 将您当前的登录状态自动保存。")
        return
        
    # 1. 退出进程
    kill_chatgpt_processes()
    
    # 2. 备份当前
    if current:
        print(f"正在增量备份当前账号 '{current}' 的最新状态...")
        backup_profile(current)
        
    # 3. 还原目标
    if restore_profile(target_profile):
        print(f"{GREEN}成功切换到账号 '{target_profile}'！{RESET}")
        
    # 4. 重新拉起
    print("正在重新打开 ChatGPT 应用程序...")
    subprocess.run(["open", "-a", "/Applications/ChatGPT.app"])

def cmd_del(target_profile):
    # 1. 获取所有可用的 Profile 列表
    parent_dir = os.path.expanduser("~/Library/Application Support")
    prefix = os.path.basename(BACKUP_PREFIX)
    profiles = []
    if os.path.exists(parent_dir):
        for name in os.listdir(parent_dir):
            if name.startswith(prefix + "_"):
                profiles.append(name[len(prefix) + 1:])
                
    # 2. 匹配逻辑 (优先精确匹配，再进行模糊匹配)
    if target_profile in profiles:
        pass
    else:
        matched = []
        for p in profiles:
            if target_profile.lower() in p.lower():
                matched.append(p)
                
        if len(matched) == 1:
            resolved_profile = matched[0]
            print(f"智能匹配到唯一 Profile: '{resolved_profile}'")
            target_profile = resolved_profile
        elif len(matched) > 1:
            print(f"{RED}错误: 发现多个匹配的 Profile: {', '.join(matched)}{RESET}")
            print("请指定更精确的名称。")
            return
        else:
            print(f"{RED}错误: 账号 Profile '{target_profile}' 不存在。{RESET}")
            return

    # 3. 检查是否为当前活跃账号
    current = get_current_active()
    if current == target_profile:
        print(f"{RED}错误: 账号 '{target_profile}' 是当前正在活跃使用的账号。{RESET}")
        print("为了安全，请先使用 switch 切换到其他账号，再执行删除操作。")
        return

    # 4. 命令行确认
    try:
        ans = input(f"确定要永久删除账号 Profile '{target_profile}' 的本地备份吗？[y/N]: ")
        if ans.lower() not in ['y', 'yes']:
            print("操作已取消。")
            return
    except (KeyboardInterrupt, EOFError):
        print("\n操作已取消。")
        return

    # 5. 执行物理删除
    backup_dir = f"{BACKUP_PREFIX}_{target_profile}"
    if os.path.exists(backup_dir):
        try:
            shutil.rmtree(backup_dir)
            print(f"{GREEN}物理备份目录已成功删除。{RESET}")
        except Exception as e:
            print(f"{RED}删除备份目录失败: {e}{RESET}")
            return
            
    # 6. 从用量缓存中移除该账号记录
    usage_file = os.path.expanduser("~/.codex/accounts_usage.json")
    if os.path.exists(usage_file):
        try:
            with open(usage_file, 'r') as f:
                usage_data = json.load(f)
            if target_profile in usage_data:
                del usage_data[target_profile]
                with open(usage_file, 'w') as f:
                    json.dump(usage_data, f, indent=2)
                print(f"用量缓存记录已成功清理。")
        except Exception:
            pass

    print(f"{GREEN}账号 '{target_profile}' 已成功删除！{RESET}")

def silent_query_quota(profile_name, port=9299):
    backup_dir = f"{BACKUP_PREFIX}_{profile_name}"
    app_support_backup = os.path.join(backup_dir, "app_support", "Default")
    
    if not os.path.exists(app_support_backup):
        return {"status": "No Backup Data"}
        
    # 1. 准备独立的临时 userData 目录
    temp_user_data = f"/tmp/codex_query_userdata_{profile_name}"
    if os.path.exists(temp_user_data):
        shutil.rmtree(temp_user_data)
    os.makedirs(os.path.join(temp_user_data, "Default"), exist_ok=True)
    
    # 2. 把 Cookies & LocalStorage 复制过去
    for item in ["Cookies", "Cookies-journal"]:
        src = os.path.join(app_support_backup, item)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(temp_user_data, "Default", item))
            
    for folder in ["Local Storage", "Session Storage"]:
        src_folder = os.path.join(app_support_backup, folder)
        if os.path.exists(src_folder):
            shutil.copytree(src_folder, os.path.join(temp_user_data, "Default", folder))
            
    # 3. 读取 auth.json 里的 access_token 用于 Bearer 鉴权
    access_token = ""
    auth_path = os.path.join(backup_dir, "auth.json")
    if os.path.exists(auth_path):
        try:
            with open(auth_path, "r") as f:
                auth_data = json.load(f)
                access_token = auth_data.get("tokens", {}).get("access_token", "")
        except Exception:
            pass

    # 4. headless 模式拉起独立进程 (使用绕过 Cloudflare 检测的参数)
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
    
    def send_cdp(ws, method, params=None, req_id=1):
        payload = {"id": req_id, "method": method}
        if params:
            payload["params"] = params
        ws.send(json.dumps(payload))
        while True:
            res_frame = json.loads(ws.recv())
            if res_frame.get("id") == req_id:
                return res_frame
                
    limits_info = {}
    try:
        # 5. 等待拉起 (增加到 4 秒以确保端口已监听就绪)
        time.sleep(4.0)
        
        # 6. 获取已有标签页，如果没有则新建一个
        list_url = f"http://127.0.0.1:{port}/json"
        req = urllib.request.Request(list_url, headers={"User-Agent": "curl/7.88.1"})
        ws_url = None
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                tabs = json.loads(response.read().decode('utf-8'))
                if tabs:
                    # 优先寻找包含 chatgpt 的 tab，否则使用第一个
                    for t in tabs:
                        if "chatgpt.com" in t.get("url", ""):
                            ws_url = t.get("webSocketDebuggerUrl")
                            break
                    if not ws_url:
                        ws_url = tabs[0].get("webSocketDebuggerUrl")
        except Exception:
            pass
            
        if not ws_url:
            # 如果没有，则发送 PUT 新建标签页
            new_url = f"http://127.0.0.1:{port}/json/new"
            req = urllib.request.Request(new_url, method="PUT", headers={"User-Agent": "curl/7.88.1"})
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    tab_data = json.loads(response.read().decode('utf-8'))
                    ws_url = tab_data.get("webSocketDebuggerUrl")
            except Exception as e:
                return {"status": f"CDP Connection Failed: {e}"}
                
        if not ws_url:
            return {"status": "Failed to get tab websocket url"}
            
        # 7. 使用 WebSocket 导航并读取内容
        ws = websocket.create_connection(ws_url, timeout=15)
        
        # 启用 Runtime
        send_cdp(ws, "Runtime.enable", req_id=1)
        
        # 发送导航命令到 chatgpt.com。
        # 注意: 导航大页面可能耗时极长，我们通过 WebSocket 异步发送，并不阻塞等待 load 完成，以防止超时。
        ws.send(json.dumps({
            "id": 2,
            "method": "Page.navigate",
            "params": {"url": "https://chatgpt.com/"}
        }))
        
        # 强行等待 4.5 秒，让浏览器跳转并准备好基本的 chatgpt.com 的同源 Cookie 上下文域。
        time.sleep(4.5)
        
        # 8. 在页面上下文中 fetch backend-api/codex/usage (使用异步全局变量轮询机制)
        # 清空可能的旧数据
        send_cdp(ws, "Runtime.evaluate", {"expression": "window.__my_res = null;"}, req_id=3)
        
        fetch_js = f"""
        fetch('https://chatgpt.com/backend-api/codex/usage', {{
            headers: {{ 'Authorization': 'Bearer {access_token}' }}
        }}).then(r => r.json().catch(() => r.text())).then(data => {{
            window.__my_res = data;
        }}).catch(e => {{
            window.__my_res = 'FETCH_ERROR: ' + e.message;
        }})
        """
        send_cdp(ws, "Runtime.evaluate", {"expression": fetch_js}, req_id=4)
        
        # 轮询 12 次 (最大等待 6 秒) 获取结果
        raw_val = None
        for i in range(12):
            time.sleep(0.5)
            res = send_cdp(ws, "Runtime.evaluate", {
                "expression": "window.__my_res",
                "returnByValue": True
            }, req_id=5 + i)
            val = res.get("result", {}).get("result", {}).get("value")
            if val is not None:
                raw_val = val
                break
                
        ws.close()
        
        # 9. 解析获取的数据
        if raw_val:
            if isinstance(raw_val, dict):
                if "rate_limit" in raw_val:
                    limits_info["rate_limit"] = raw_val["rate_limit"]
                    limits_info["status"] = "OK"
                elif "detail" in raw_val:
                    if "unauthorized" in str(raw_val).lower():
                        limits_info["status"] = "Token Expired"
                    else:
                        limits_info["status"] = f"API Error: {raw_val.get('detail')}"
                else:
                    limits_info["status"] = "Parse Error"
            else:
                raw_text = str(raw_val)
                if "challenge" in raw_text.lower() or "cloudflare" in raw_text.lower():
                    limits_info["status"] = "Blocked by Cloudflare"
                elif "unauthorized" in raw_text.lower() or "token is missing" in raw_text.lower():
                    limits_info["status"] = "Token Expired"
                else:
                    limits_info["status"] = "Parse Error"
        else:
            limits_info["status"] = "Empty Response"
            
    except Exception as e:
        limits_info["status"] = f"Error: {e}"
    finally:
        # 10. 彻底清理无头进程
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()
        
        # 清理临时 userData
        try:
            shutil.rmtree(temp_user_data)
        except Exception:
            pass
            
    return limits_info

def load_usage_cache():
    if os.path.exists(USAGE_CACHE_FILE):
        try:
            with open(USAGE_CACHE_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_usage_cache(cache):
    try:
        os.makedirs(CODEX_HOME, exist_ok=True)
        with open(USAGE_CACHE_FILE, 'w') as f:
            json.dump(cache, f, indent=2)
    except Exception:
        pass

def cmd_list(refresh=False):
    profiles = get_profiles()
    current = get_current_active()
    
    if not profiles:
        print(f"{YELLOW}未发现任何账号 Profile。请按照说明配置备份。{RESET}")
        return
        
    usage_cache = load_usage_cache()
    
    if refresh:
        print("正在静默现查所有账号的限额与额度 (后台无头执行，不打扰当前客户端)...")
        # 对每一个 profile 现查
        for i, p in enumerate(profiles):
            print(f"  [{i+1}/{len(profiles)}] 正在查询账号: {p} ...", end="", flush=True)
            info = silent_query_quota(p)
            usage_cache[p] = {
                "limits": info,
                "timestamp": time.time()
            }
            print(f" {GREEN}完成{RESET}")
        save_usage_cache(usage_cache)
        print("")

    # 5. 打印表格 (宽版完美对齐)
    print(f"\n{BOLD}{CYAN}=== ChatGPT/Codex 账号管理列表 ==={RESET}")
    
    col_active = 10
    col_profile = 30
    col_email = 30
    col_plan = 8
    col_until = 26
    
    header = f"{'Active':<{col_active}}{'Profile Name':<{col_profile}}{'Email':<{col_email}}{'Plan':<{col_plan}}{'Subscription Until (UTC)':<{col_until}}{'Quota / Limit Info'}"
    print("-" * len(header))
    print(f"{BOLD}{header}{RESET}")
    print("-" * len(header))
    
    for p in profiles:
        backup_dir = f"{BACKUP_PREFIX}_{p}"
        auth_path = os.path.join(backup_dir, "auth.json")
        
        email = "Unknown"
        plan = "Unknown"
        until = "Unknown"
        is_active = (p == current)
        
        # 本地解析 JWT 凭证
        if os.path.exists(auth_path):
            try:
                with open(auth_path, 'r') as f:
                    auth_data = json.load(f)
                id_token = auth_data.get("tokens", {}).get("id_token")
                if id_token:
                    payload = decode_jwt_payload(id_token)
                    if payload:
                        email = payload.get("email", "Unknown")
                        auth_sec = payload.get("https://api.openai.com/auth", {})
                        plan = auth_sec.get("chatgpt_plan_type", "free").upper()
                        until_raw = auth_sec.get("chatgpt_subscription_active_until", "Unknown")
                        if until_raw != "Unknown":
                            until = until_raw.split('T')[0]
            except Exception:
                pass
                
        # 读取用量缓存
        usage_data = usage_cache.get(p, {})
        limits = usage_data.get("limits", {})
        limits_status = limits.get("status", "No Data")
        
        # 格式化用量限制展示
        quota_str = ""
        if limits_status == "Token Expired":
            quota_str = f"{RED}Token Expired ❌{RESET}"
        elif limits_status == "Blocked by Cloudflare":
            quota_str = f"{YELLOW}CF Check Failed ⚠️{RESET}"
        elif limits_status == "No Data":
            quota_str = f"{YELLOW}No Data (请使用 --refresh 现查){RESET}"
        elif limits_status == "OK" and "rate_limit" in limits:
            rl = limits["rate_limit"]
            pw = rl.get("primary_window", {})
            sw = rl.get("secondary_window", {})
            
            # 计算剩余百分比 (剩余 = 100 - 已用)
            pct_5h = 100 - pw.get("used_percent", 0)
            pct_1w = 100 - sw.get("used_percent", 0)
            
            # 如果剩余配额偏低，以红色警示；健康状态下以绿色呈现
            pct_5h_str = f"{RED}{pct_5h}%{RESET}" if pct_5h <= 15 else f"{GREEN}{pct_5h}%{RESET}"
            pct_1w_str = f"{RED}{pct_1w}%{RESET}" if pct_1w <= 15 else f"{GREEN}{pct_1w}%{RESET}"
            
            # 格式化 5h 的重置时间戳 (转换为本地 12小时 AM/PM 格式，如 5:52 PM)
            r5h_str = ""
            r5h_ts = pw.get("reset_at")
            if r5h_ts:
                try:
                    dt_5h = datetime.datetime.fromtimestamp(r5h_ts)
                    r5h_str = " " + dt_5h.strftime("%I:%M %p").lstrip('0')
                except Exception:
                    pass
                    
            # 格式化 1w 的重置时间戳 (转换为英文月日格式，如 Jul 18)
            r1w_str = ""
            r1w_ts = sw.get("reset_at")
            if r1w_ts:
                try:
                    dt_1w = datetime.datetime.fromtimestamp(r1w_ts)
                    r1w_str = " " + dt_1w.strftime("%b %d")
                except Exception:
                    pass
                    
            quota_str = f"5小时: {pct_5h_str}{r5h_str} | 1周: {pct_1w_str}{r1w_str}"
        else:
            quota_str = limits_status
                
        # 加上更新时间指示
        ts = usage_data.get("timestamp")
        if ts and limits_status != "No Data":
            mins_ago = int((time.time() - ts) / 60)
            if mins_ago == 0:
                quota_str += " (刚刚更新)"
            else:
                quota_str += f" ({mins_ago}分钟前)"
                
        # 纯文本对齐，避免 ANSI 导致排版崩塌
        active_val = "* ACTIVE" if is_active else ""
        p_val = p
        email_val = email
        plan_val = plan
        until_val = until
        quota_val = quota_str  # quota_str 放在最后，即使带颜色也不会影响前面对齐
        
        # 组装行
        line = f"{active_val:<{col_active}}{p_val:<{col_profile}}{email_val:<{col_email}}{plan_val:<{col_plan}}{until_val:<{col_until}}{quota_val}"
        
        # 对活动账号标志进行着色
        if "* ACTIVE" in line:
            line = line.replace("* ACTIVE", f"{GREEN}* ACTIVE{RESET}")
            
        print(line)
        
    print("-" * len(header))
    if not refresh:
        print(f"提示: 以上额度用量基于缓存展示。运行 {BOLD}python3 codex_mgr.py list --refresh{RESET} 可静默现查最新额度。")
    print(f"提示: 后台守护服务每半小时会自动保活并刷新用量缓存。")

def cmd_wakeup():
    profiles = get_profiles()
    if not profiles:
        print("未发现任何可唤醒的账号 Profile。")
        return
        
    original = get_current_active()
    print(f"=== 开始批量唤醒保活所有 ChatGPT 账号 (总共 {len(profiles)} 个) ===")
    
    # 确保当前活跃账号的数据先保存
    if original:
        print(f"正在保存当前活跃账号 '{original}' 的最新状态...")
        backup_profile(original)
        
    usage_cache = load_usage_cache()
    
    for i, p in enumerate(profiles):
        print("\n-----------------------------------")
        print(f"[{i+1}/{len(profiles)}] 正在载入并刷新账号: {p}")
        restore_profile(p)
        
        # 启动客户端暴露调试端口，主动执行 models fetch 刷新 Token 并获取额度
        print("正在以无头静默模式拉起客户端进行心跳刷新...")
        info = silent_query_quota(p)
        usage_cache[p] = {
            "limits": info,
            "timestamp": time.time()
        }
        
        # 主动备份更新后的 Token (因为 silent_query_quota 可能促使客户端在后台更新了 auth.json)
        print("正在将刷新后的最新状态存入备份...")
        backup_profile(p)
        
    # 保存用量缓存
    save_usage_cache(usage_cache)
    
    print("\n-----------------------------------")
    if original:
        print(f"正在恢复切换到最初的活跃账号: {original}")
        restore_profile(original)
        # 重新正常拉起主客户端
        subprocess.run(["open", "-a", "/Applications/ChatGPT.app"])
    else:
        print("所有账号唤醒完成！")

def cmd_daemon():
    print(f"{GREEN}Codex 账号管理后台守护进程已启动。{RESET}")
    print("将每隔半小时自动批量唤醒保活并更新额度缓存...")
    try:
        while True:
            cmd_wakeup()
            print(f"\n批量唤醒完成。等待半小时以进行下一次保活...")
            time.sleep(1800)
    except KeyboardInterrupt:
        print("\n守护进程已安全退出。")

def print_help():
    print(f"""
ChatGPT/Codex 多账号管理器 CLI

使用方法:
  python3 codex_mgr.py <command> [args]

可用命令:
  list              列出所有账号 Profile 及其订阅到期日、用量限额 (读取本地缓存)
  list --refresh    静默现查所有账号的限额与额度 (不打扰当前客户端，不弹窗)
  add <name>        将您当前的登录态备份另存为一个全新的 Profile 账号
  switch <name>     一键备份当前账号，无缝切换到目标账号并重新打开客户端
  del/remove <name> 永久删除指定账号 Profile 的本地备份和用量缓存
  wakeup            手动执行一次批量刷新保活与用量更新
  daemon            在当前终端启动常驻后台守护进程 (建议使用 wakeup_daemon.sh 启动)
""")

def main():
    if len(sys.argv) < 2:
        print_help()
        sys.exit(1)
        
    cmd = sys.argv[1]
    
    if cmd == "list":
        refresh = False
        if len(sys.argv) > 2 and sys.argv[2] in ["--refresh", "-r"]:
            refresh = True
        cmd_list(refresh)
    elif cmd == "add":
        profile = sys.argv[2] if len(sys.argv) > 2 else None
        cmd_add(profile)
    elif cmd == "switch":
        if len(sys.argv) < 3:
            print("错误: 请指定要切换的目标账号 Profile 名称。")
            print("示例: python3 codex_mgr.py switch account1")
            sys.exit(1)
        cmd_switch(sys.argv[2])
    elif cmd in ["del", "remove"]:
        if len(sys.argv) < 3:
            print("错误: 请指定要删除的账号 Profile 名称。")
            print("示例: python3 codex_mgr.py del account1")
            sys.exit(1)
        cmd_del(sys.argv[2])
    elif cmd == "wakeup":
        cmd_wakeup()
    elif cmd == "daemon":
        cmd_daemon()
    else:
        print_help()

if __name__ == "__main__":
    main()
