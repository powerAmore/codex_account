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
    # 覆盖主程序、Framework 子进程、内置 codex app-server、Computer Use 与 Chrome 扩展宿主
    patterns = [
        "ChatGPT.app",
        "ChatGPT",
        "Codex Framework",
        "Codex (Service)",
        "Codex (Renderer)",
        "/Contents/Resources/codex",
        "SkyComputerUseService",
        "ChatGPT for Chrome",
        "com.openai.codex",
    ]
    for pat in patterns:
        subprocess.run(["pkill", "-f", pat], capture_output=True)
    time.sleep(1.5)

    # 双重检查：仍有主程序或 app-server 则强杀
    still_running = False
    for pat in ["ChatGPT.app", "/Contents/Resources/codex", "SkyComputerUseService"]:
        res = subprocess.run(["pgrep", "-f", pat], capture_output=True)
        if res.returncode == 0:
            still_running = True
            break
    if still_running:
        print(f"{YELLOW}提示: 检测到 ChatGPT/Codex 仍有残余进程，强行关闭中...{RESET}")
        for pat in patterns:
            subprocess.run(["pkill", "-9", "-f", pat], capture_output=True)
        time.sleep(1.0)

def backup_profile(profile_name, quiet=False):
    """将当前登录态备份到指定 Profile 目录。返回 True 表示成功，False 表示失败。"""
    backup_dir = f"{BACKUP_PREFIX}_{profile_name}"
    try:
        os.makedirs(backup_dir, exist_ok=True)
    except Exception as e:
        if not quiet:
            print(f"[{RED}ERROR{RESET}] 无法创建备份目录 '{backup_dir}': {e}")
        return False

    success = True

    # 备份 App Support 核心文件
    app_support_backup = os.path.join(backup_dir, "app_support", "Default")
    os.makedirs(app_support_backup, exist_ok=True)

    default_dir = os.path.join(APP_SUPPORT_DIR, "Default")
    if os.path.exists(default_dir):
        if not quiet:
            print(f"正在增量备份活跃账号 Cookies & 本地存储...")
        # 仅备份核心认证文件以极大地压缩体积
        for item in ["Cookies", "Cookies-journal"]:
            src = os.path.join(default_dir, item)
            if os.path.exists(src):
                try:
                    shutil.copy2(src, os.path.join(app_support_backup, item))
                except Exception as e:
                    if not quiet:
                        print(f"[{RED}ERROR{RESET}] 备份 {item} 失败: {e}")
                    success = False

        # 复制 Local Storage / Session Storage
        for folder in ["Local Storage", "Session Storage"]:
            src_folder = os.path.join(default_dir, folder)
            dst_folder = os.path.join(app_support_backup, folder)
            if os.path.exists(src_folder):
                try:
                    if os.path.exists(dst_folder):
                        shutil.rmtree(dst_folder)
                    shutil.copytree(src_folder, dst_folder)
                except Exception as e:
                    if not quiet:
                        print(f"[{RED}ERROR{RESET}] 备份 '{folder}' 失败: {e}")
                    success = False

    # 备份 auth.json
    if os.path.exists(AUTH_FILE):
        try:
            shutil.copy2(AUTH_FILE, os.path.join(backup_dir, "auth.json"))
        except Exception as e:
            if not quiet:
                print(f"[{RED}ERROR{RESET}] 备份 auth.json 失败: {e}")
            success = False

    return success

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

    # 2. 在做任何操作之前，先扫描所有现有 Profile，检查是否已有相同邮箱账号
    #    防止同一账号以不同名字被重复保存
    if detected_email:
        existing_profiles = get_profiles()
        for ep in existing_profiles:
            ep_auth = os.path.join(f"{BACKUP_PREFIX}_{ep}", "auth.json")
            if os.path.exists(ep_auth):
                try:
                    with open(ep_auth, "r") as f:
                        ep_data = json.load(f)
                    ep_token = ep_data.get("tokens", {}).get("id_token")
                    if ep_token:
                        ep_payload = decode_jwt_payload(ep_token)
                        if ep_payload.get("email") == detected_email:
                            print(f"{YELLOW}提示: 当前登录的账号 ({detected_email}) 已经以 Profile '{ep}' 保存过了。{RESET}")
                            print(f"如果您想更新该 Profile 的备份，请直接切换到它：python3 codex_mgr.py switch {ep}")
                            print("无需重复添加。")
                            return
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

            if candidate_name:
                backup_dir_check = f"{BACKUP_PREFIX}_{candidate_name}"
                if os.path.exists(backup_dir_check):
                    # 名字已存在但邮箱不同（上方已排除邮箱重复），说明是同名不同人，加数字后缀
                    suffix = 2
                    while os.path.exists(f"{BACKUP_PREFIX}_{candidate_name}_{suffix}"):
                        suffix += 1
                    candidate_name = f"{candidate_name}_{suffix}"
                profile_name = candidate_name
                print(f"检测到当前登录的真实姓名: '{detected_name}'，自动命名为: '{profile_name}'")

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

    # 3. 退出进程以保证文件拷贝安全完整
    print(f"正在安全关闭 ChatGPT / Codex 进程...")
    kill_chatgpt_processes()

    # 4. 对当前可能活跃的旧 Profile 执行最新的增量备份
    current = get_current_active()
    if current:
        print(f"正在增量备份当前活跃账号 '{current}' 的最新状态...")
        backup_profile(current)

    # 5. 创建新 Profile 目录并备份当前数据
    print(f"正在将当前登录态保存为新 Profile: '{profile_name}'...")
    os.makedirs(backup_dir, exist_ok=True)
    backup_profile(profile_name)
    set_current_active(profile_name)
    print(f"{GREEN}账号 Profile '{profile_name}' 创建并备份成功！{RESET}")


    # 6. 重新拉起客户端
    print("正在重新打开 ChatGPT 应用程序...")
    subprocess.run(["open", "-a", "/Applications/ChatGPT.app"])


def cmd_switch(target_profile):
    current = get_current_active()
    
    # 特殊指令: 切换到一个干净的“未登录”环境，用于添加/登录全新账号
    if target_profile.lower() in ["new", "--new"]:
        kill_chatgpt_processes()
        
        # 漏洞 A 修复：如果 current 标记为空，但实际上本地有已登录的数据（例如未记录的野生登录态）
        if not current:
            detected_name = None
            if os.path.exists(AUTH_FILE):
                try:
                    with open(AUTH_FILE, "r") as f:
                        auth_data = json.load(f)
                        id_token = auth_data.get("tokens", {}).get("id_token")
                        if id_token:
                            payload = decode_jwt_payload(id_token)
                            detected_name = payload.get("name")
                except Exception:
                    pass
            
            if detected_name:
                valid_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.@")
                candidate_name = "".join(c if c in valid_chars else "_" for c in detected_name)
                while "__" in candidate_name:
                    candidate_name = candidate_name.replace("__", "_")
                candidate_name = candidate_name.strip("_")
                current = candidate_name
                print(f"检测到本地有未被记录的活跃登录账号，自动识别并关联为 Profile: '{current}'")
            elif os.path.exists(AUTH_FILE) or os.path.exists(os.path.join(APP_SUPPORT_DIR, "Default", "Cookies")):
                current = "auto_saved_profile"
                print(f"检测到本地有未被记录的登录数据，已自动为您保存为安全备份: '{current}' (防止清空丢失)")
                
        if current:
            print(f"正在增量备份当前账号 '{current}' 的最新状态...")
            backup_ok = backup_profile(current)
            if not backup_ok:
                print(f"{RED}[安全中止] 账号 '{current}' 备份失败！为保护您的账号数据，已取消清空操作。{RESET}")
                print("请检查磁盘空间或文件权限后重试。")
                return
            print(f"{GREEN}备份成功，账号数据已安全保存。{RESET}")
            
        print("正在重置客户端运行环境，创造干净的“未登录”状态...")
        if os.path.exists(APP_SUPPORT_DIR):
            try:
                shutil.rmtree(APP_SUPPORT_DIR)
            except Exception as e:
                print(f"{RED}清空缓存目录失败: {e}{RESET}")
                print("操作已中止，您的账号数据未受影响。")
                return

        os.makedirs(APP_SUPPORT_DIR, exist_ok=True)

        # 关键：必须同步清除 ~/.codex/auth.json。
        # 新版 ChatGPT/Codex 客户端会通过内置 codex app-server 读取该文件自动恢复登录态；
        # 若只清空 Application Support/Codex 而保留 auth.json，重启后仍会显示旧账号。
        if os.path.exists(AUTH_FILE):
            try:
                os.remove(AUTH_FILE)
                print("已清除 CLI/App 共享凭证文件 auth.json（防止自动恢复旧登录）。")
            except Exception as e:
                print(f"{RED}清除 auth.json 失败: {e}{RESET}")
                print("操作已中止。请检查文件权限后重试，以免客户端继续自动登录旧账号。")
                return

        if os.path.exists(ACTIVE_FILE):
            try:
                os.remove(ACTIVE_FILE)
            except Exception:
                pass

        print(f"\n{GREEN}成功重置客户端！当前已处于干净的“未登录”状态。{RESET}")
        print("正在拉起 ChatGPT 应用程序...")
        print(f"{BOLD}提示: 请在弹出的客户端窗口中，直接输入并登录您的第二个新账号。{RESET}")
        print(f"提示: 登录成功后，在终端运行 {BOLD}python3 codex_mgr.py add{RESET} 即可将此新账号自动完成备份。")

        subprocess.run(["open", "-a", "/Applications/ChatGPT.app"])
        return

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

    if current == target_profile:
        print(f"您当前已处于账号 '{target_profile}' 下！正在重新打开 App...")
        subprocess.run(["open", "-a", "/Applications/ChatGPT.app"])
        return
        
    backup_dir = f"{BACKUP_PREFIX}_{target_profile}"
    if not os.path.exists(backup_dir):
        print(f"{RED}错误: 账号 Profile '{target_profile}' 不存在。{RESET}")
        print(f"提示: 若要登录并添加新账号，请运行: {BOLD}python3 codex_mgr.py switch new{RESET}")
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

def find_free_port():
    """动态获取一个当前空闲可用的 TCP 端口。"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

def silent_query_quota(profile_name):
    backup_dir = f"{BACKUP_PREFIX}_{profile_name}"
    app_support_backup = os.path.join(backup_dir, "app_support", "Default")
    
    if not os.path.exists(app_support_backup):
        return {"status": "No Backup Data"}
        
    # 动态分配端口，避开端口竞争与 TIME_WAIT 锁定问题
    try:
        port = find_free_port()
    except Exception:
        port = 9299 # 兜底端口
        
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
            
    # 3. 读取 auth.json 里的 access_token（仅作为备用，主要依赖 Cookie 鉴权）
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
                
    limits_info = {}
    try:
        # 5. 等待拉起 (增加到 4 秒以确保端口已监听就绪)
        time.sleep(4.0)
        
        # 显式禁止代理，避免 http_proxy/https_proxy 干扰本地 127.0.0.1 的 CDP 端口通信
        no_proxy_handler = urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(no_proxy_handler)
        
        # 6. 获取已有标签页，如果没有则新建一个
        list_url = f"http://127.0.0.1:{port}/json"
        req = urllib.request.Request(list_url, headers={"User-Agent": "curl/7.88.1"})
        ws_url = None
        try:
            with opener.open(req, timeout=5) as response:
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
                with opener.open(req, timeout=5) as response:
                    tab_data = json.loads(response.read().decode('utf-8'))
                    ws_url = tab_data.get("webSocketDebuggerUrl")
            except Exception as e:
                return {"status": f"CDP Connection Failed: {e}"}
                
        if not ws_url:
            return {"status": "Failed to get tab websocket url"}
            
        # 7. 使用 WebSocket 导航并读取内容 (传入 skip_proxy=True 屏蔽系统代理干扰)
        ws = websocket.create_connection(ws_url, timeout=15, skip_proxy=True)
        
        # 启用 Runtime 并将标签页置于前台激活，防止由于 Tab 处于背景而被 Chrome 节流限制(Throttling)挂起网络
        send_cdp(ws, "Runtime.enable")
        send_cdp(ws, "Page.bringToFront")
        
        # 发送导航命令到 chatgpt.com。
        # 注意: 导航大页面可能耗时极长，我们通过 WebSocket 异步发送，并不阻塞等待 load 完成，以防止超时。
        ws.send(json.dumps({
            "id": 99999,  # 用一个特殊的较大 ID 异步发送导航命令，不占用自增 ID 轮询
            "method": "Page.navigate",
            "params": {"url": "https://chatgpt.com/"}
        }))
        
        # 强制等待 6.0 秒以确保新页面加载定型完成，防止加载过程中产生的新页面重定向冲刷清空注入的 JS 代码与变量
        time.sleep(6.0)
        
        # 8. 在页面上下文中 fetch backend-api/codex/usage (使用异步全局变量轮询机制)
        # 清空可能的旧数据，并把备用 Bearer Token 注入到页面全局变量
        send_cdp(ws, "Runtime.evaluate", {"expression": "window.__my_res = null;"})
        # 注入备用 token（用 JSON 序列化避免 JS 注入问题）
        token_json = json.dumps(access_token)
        send_cdp(ws, "Runtime.evaluate", {
            "expression": f"window.__bearer_token = {token_json};"
        })
        
        fetch_js = """
        (function() {
            const safeParse = (r) => r.text().then(text => {
                try { return JSON.parse(text); } catch(e) { return text; }
            });

            // 主要策略：用浏览器本身的 Cookie 请求（Cookie 有效期远比 access_token 长）
            // 这样即使 auth.json 里的 access_token 已过期，只要 Session Cookie 还有效，保活就不会失败
            fetch('https://chatgpt.com/backend-api/codex/usage', {
                credentials: 'include'
            }).then(r => {
                const status = r.status;
                return safeParse(r).then(data => ({ data, status }));
            }).then(({ data, status }) => {
                // 若 Cookie 鉴权成功，直接返回
                if (status !== 401) {
                    window.__my_res = data;
                    return;
                }
                // Cookie 鉴权返回 401，尝试用 Bearer Token 作为备用
                const bearerToken = window.__bearer_token;
                if (!bearerToken) {
                    window.__my_res = data;  // 没有备用 token，直接返回 401 结果
                    return;
                }
                return fetch('https://chatgpt.com/backend-api/codex/usage', {
                    headers: { 'Authorization': 'Bearer ' + bearerToken }
                }).then(r2 => safeParse(r2)).then(data2 => {
                    window.__my_res = data2;
                });
            }).catch(e => {
                window.__my_res = 'FETCH_ERROR: ' + e.message;
            });
        })();
        """
        eval_res = send_cdp(ws, "Runtime.evaluate", {"expression": fetch_js})
        # 检查 JS 脚本本身是否存在语法错误或执行错误
        if "exceptionDetails" in eval_res.get("result", {}):
            exc = eval_res["result"]["exceptionDetails"]
            return {"status": f"JS Evaluate Error: {exc.get('text', 'Unknown Error')} (line {exc.get('lineNumber', 0)})"}
        
        # 轮询 20 次 (最大等待 10 秒) 获取结果
        raw_val = None
        for i in range(20):
            time.sleep(0.5)
            res = send_cdp(ws, "Runtime.evaluate", {
                "expression": "window.__my_res",
                "returnByValue": True
            })
            
            # 校验轮询本身的错误
            if "exceptionDetails" in res.get("result", {}):
                exc = res["result"]["exceptionDetails"]
                return {"status": f"JS Poll Error: {exc.get('text', 'Unknown Error')}"}
                
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
                elif "error" in raw_val:
                    # OpenAI 标准错误格式: {"error": {"code": "token_invalidated", ...}, "status": 401}
                    error_obj = raw_val.get("error", {})
                    error_code = str(error_obj.get("code", "")).lower()
                    http_status = raw_val.get("status", 0)
                    if http_status == 401 or "token" in error_code or "invalid" in error_code or "unauthorized" in error_code:
                        limits_info["status"] = "Token Expired"
                    else:
                        limits_info["status"] = f"API Error: {error_obj.get('message', str(raw_val))}"
                elif "detail" in raw_val:
                    if "unauthorized" in str(raw_val).lower():
                        limits_info["status"] = "Token Expired"
                    else:
                        limits_info["status"] = f"API Error: {raw_val.get('detail')}"
                else:
                    limits_info["status"] = "Parse Error"
            else:
                raw_text = str(raw_val)
                if "timeout" in raw_text.lower() or "abort" in raw_text.lower():
                    limits_info["status"] = "Network Timeout"
                elif "challenge" in raw_text.lower() or "cloudflare" in raw_text.lower():
                    limits_info["status"] = "Blocked by Cloudflare"
                elif "unauthorized" in raw_text.lower() or "token is missing" in raw_text.lower() or "token_invalidated" in raw_text.lower():
                    limits_info["status"] = "Token Expired"
                else:
                    limits_info["status"] = f"Parse Error: {raw_text[:100]}"
        else:
            limits_info["status"] = "Network Timeout"
            
    except Exception as e:
        limits_info["status"] = f"Error: {e}"
    finally:
        # 10. 彻底清理无头进程
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()

        # 11. 把无头实例运行后可能更新的 Cookies / LocalStorage 拷回备份目录
        #     （服务器可能下发了新的滚动 Session Cookie，不拷回则下次唤醒用的是旧 Cookie）
        try:
            tmp_default = os.path.join(temp_user_data, "Default")
            backup_default = os.path.join(backup_dir, "app_support", "Default")
            os.makedirs(backup_default, exist_ok=True)

            for item in ["Cookies", "Cookies-journal"]:
                src = os.path.join(tmp_default, item)
                dst = os.path.join(backup_default, item)
                if os.path.exists(src):
                    shutil.copy2(src, dst)

            for folder in ["Local Storage", "Session Storage"]:
                src_folder = os.path.join(tmp_default, folder)
                dst_folder = os.path.join(backup_default, folder)
                if os.path.exists(src_folder):
                    if os.path.exists(dst_folder):
                        shutil.rmtree(dst_folder)
                    shutil.copytree(src_folder, dst_folder)
        except Exception as e:
            # 拷回失败不影响主流程，仅记录
            pass

        # 12. 清理临时 userData
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

def cmd_list(refresh=False, target_profile=None):
    profiles = get_profiles()
    current = get_current_active()
    
    if not profiles:
        print(f"{YELLOW}未发现任何账号 Profile。请按照说明配置备份。{RESET}")
        return
        
    if target_profile and target_profile not in profiles:
        print(f"{RED}错误: 账号 Profile '{target_profile}' 不存在。{RESET}")
        return
        
    usage_cache = load_usage_cache()
    
    if refresh:
        to_refresh = [target_profile] if target_profile else profiles
        # 在现查开始前，如果要刷新的账号里包含当前的 ACTIVE 账号，自动增量备份其最新登录态
        if current and current in to_refresh:
            backup_profile(current, quiet=True)
        print("正在静默现查指定账号的限额与额度..." if target_profile else "正在静默现查所有账号的限额与额度 (后台无头执行，不打扰当前客户端)...")
        # 对每一个 profile 现查
        for i, p in enumerate(to_refresh):
            print(f"  [{i+1}/{len(to_refresh)}] 正在查询账号: {p} ...", end="", flush=True)
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
    col_profile = 25
    col_email = 45
    col_plan = 8
    col_until = 22
    
    header = f"{'Active':<{col_active}}{'Profile Name':<{col_profile}}{'Email':<{col_email}}{'Plan':<{col_plan}}{'Subscription Until':<{col_until}}{'Quota / Limit Info'}"
    print("-" * 135)
    print(f"{BOLD}{header}{RESET}")
    print("-" * 135)
    
    for p in profiles:
        if target_profile and p != target_profile:
            continue
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
        elif limits_status == "Network Timeout":
            quota_str = f"{YELLOW}Network Timeout ⚠️{RESET}"
        elif limits_status == "Blocked by Cloudflare":
            quota_str = f"{YELLOW}CF Check Failed ⚠️{RESET}"
        elif limits_status == "No Data":
            quota_str = f"{YELLOW}No Data (请使用 --refresh 现查){RESET}"
        elif limits_status == "OK" and "rate_limit" in limits:
            rl = limits.get("rate_limit") or {}
            parts = []
            
            for key in ["primary_window", "secondary_window"]:
                win = rl.get(key)
                if not isinstance(win, dict) or not win:
                    continue
                
                # 计算剩余百分比 (剩余 = 100 - 已用)
                used_pct = win.get("used_percent", 0)
                pct_left = 100 - used_pct
                
                # 如果剩余配额偏低，以红色警示；健康状态下以绿色呈现
                pct_str = f"{RED}{pct_left}%{RESET}" if pct_left <= 15 else f"{GREEN}{pct_left}%{RESET}"
                
                # 重置时间
                r_str = ""
                r_ts = win.get("reset_at")
                window_seconds = win.get("limit_window_seconds", 0)
                
                if r_ts:
                    try:
                        dt = datetime.datetime.fromtimestamp(r_ts)
                        # 如果是短期窗口（小于1天），用 12 小时制显示时间 (如 5:52 PM)
                        if window_seconds < 86400:
                            r_str = " " + dt.strftime("%I:%M %p").lstrip('0')
                        else:
                            # 长期窗口（大于等于1天），用月日显示 (如 Jul 20)
                            r_str = " " + dt.strftime("%b %d")
                    except Exception:
                        pass
                
                win_name = format_window_name(window_seconds)
                parts.append(f"{win_name}: {pct_str}{r_str}")
                
            quota_str = " | ".join(parts) if parts else "OK"
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
        
    print("-" * 135)
    if not refresh:
        print(f"提示: 以上额度用量基于缓存展示。运行 {BOLD}python3 codex_mgr.py list --refresh{RESET} 可静默现查最新额度。")
    print(f"提示: 后台守护服务每半小时会自动保活并刷新用量缓存。")

def format_window_name(seconds):
    """根据秒数动态计算人性化的窗口长度名称。"""
    if not seconds:
        return "用量"
    
    hours = seconds / 3600
    days = hours / 24
    
    if seconds == 604800:
        return "1周"
    if seconds == 18000:
        return "5小时"
    if seconds == 21600:
        return "6小时"
        
    if days >= 1 and hours % 24 == 0:
        return f"{int(days)}天"
    if hours >= 1 and seconds % 3600 == 0:
        return f"{int(hours)}小时"
    return f"{seconds}秒"

def _pretty_status_desc(status):
    """把接口底层的英文/异常状态翻译并附带人机友好的详细解释。"""
    if not status:
        return "Unknown"
    if status == "OK":
        return "OK"
    if status == "Token Expired":
        return "Token Expired (Session已失效/在其他设备被踢，需重新登录)"
    if status == "Network Timeout":
        return "Network Timeout (接口请求超时，请检查您的代理/节点速度)"
    if status == "Blocked by Cloudflare":
        return "Blocked by Cloudflare (被 OpenAI 防机器人墙拦截，请尝试切换干净的节点)"
    if status == "Empty Response":
        return "Empty Response (服务器没有返回任何数据)"
    
    # 模糊匹配带有详情的状态
    if status.startswith("Parse Error"):
        detail = status.replace("Parse Error:", "").strip()
        if not detail:
            return "Parse Error (服务器返回的结构不符合用量接口标准)"
        return f"Parse Error (接口返回解析失败，响应样本: {detail})"
        
    if status.startswith("API Error:"):
        detail = status.replace("API Error:", "").strip()
        return f"API Error (OpenAI API 报错: {detail})"
        
    if status.startswith("Error:"):
        detail = status.replace("Error:", "").strip()
        return f"Runtime Error (网络连接/CDP通信或底层进程异常: {detail})"
        
    return status

def _log_status(ok, detail=""):
    """格式化步骤结果：OK / FAIL + 可选细节（避免 OK OK 重复）。"""
    mark = "OK" if ok else "FAIL"
    detail = (detail or "").strip()
    if not detail or detail == mark:
        return mark
    pretty_detail = _pretty_status_desc(detail)
    return f"{mark}  {pretty_detail}"

def _is_query_ok(info):
    """额度/心跳查询是否成功。"""
    return (info or {}).get("status") == "OK"


def cmd_wakeup():
    """
    执行一轮账号保活与额度刷新。

    处理分工：
      · 前台活跃账号：
          1) live → 备份（同步 App 正在使用的最新 Cookie/Token，不关闭 App）
          2) 无头额度查询 + Session 校验，并把可能滚动的 Cookie 写回备份
             （前台 App 自身心跳已维持 live Session；此处主要保备份与额度缓存）
      · 后台账号：
          无头心跳保活 + 额度刷新 + Cookie 回写备份（防止长期不用掉登）
    """
    sep = "=" * 60
    thin = "-" * 60

    profiles = get_profiles()
    if not profiles:
        print(sep)
        print("保活跳过: 未发现任何账号 Profile")
        print(sep)
        return

    current_active = get_current_active()
    # 防并发/防覆盖：无 active 标定通常意味着正在 switch new 登录新号
    if not current_active:
        print(sep)
        print("保活跳过: 当前无活跃账号标定")
        print("原因: 可能正处于 switch new 登录新号流程中")
        print("动作: 已跳过本轮，避免覆盖前台正在登录的数据")
        print(sep)
        return

    background_profiles = [p for p in profiles if p != current_active]
    usage_cache = load_usage_cache()
    revoked_accounts = []
    results = []  # [(role, name, ok, detail), ...]

    print(sep)
    print("保活周期开始")
    print(f"  前台活跃: {current_active}")
    if background_profiles:
        print(f"  后台账号: {', '.join(background_profiles)}  (共 {len(background_profiles)} 个)")
    else:
        print("  后台账号: (无)")
    print("  说明: 前台由 App 自身维持 live Session；本轮同步备份并校验额度/Token")
    print(thin)

    # ── 前台活跃账号 ──────────────────────────────────────────────
    print(f"[前台] {current_active}")
    sync_ok = backup_profile(current_active, quiet=True)
    print(f"  · 同步 live → 备份 ........... {_log_status(sync_ok)}")
    if not sync_ok:
        results.append(("前台", current_active, False, "备份同步失败"))
    else:
        print(f"  · 额度查询 / Session 校验 ... 进行中")
        active_info = silent_query_quota(current_active)
        status = active_info.get("status", "Unknown")
        ok = _is_query_ok(active_info)
        print(f"  · 额度查询 / Session 校验 ... {_log_status(ok, status)}")
        _check_token_status(current_active, active_info, revoked_accounts)
        usage_cache[current_active] = {"limits": active_info, "timestamp": time.time()}
        results.append(("前台", current_active, ok, status))

    # ── 后台账号 ──────────────────────────────────────────────────
    for i, p in enumerate(background_profiles, 1):
        print(f"[后台 {i}/{len(background_profiles)}] {p}")
        print(f"  · 无头心跳保活 ............. 进行中")
        info = silent_query_quota(p)
        status = info.get("status", "Unknown")
        ok = _is_query_ok(info)
        print(f"  · 无头心跳保活 ............. {_log_status(ok, status)}")
        _check_token_status(p, info, revoked_accounts)
        usage_cache[p] = {"limits": info, "timestamp": time.time()}
        results.append(("后台", p, ok, status))

    save_usage_cache(usage_cache)

    # ── 汇总 ──────────────────────────────────────────────────────
    ok_n = sum(1 for r in results if r[2])
    fail_n = len(results) - ok_n
    print(thin)
    print(f"本轮结果: 成功 {ok_n}  |  失败 {fail_n}  |  Session 失效 {len(revoked_accounts)}")
    if results:
        for role, name, ok, detail in results:
            mark = "OK" if ok else "FAIL"
            pretty_detail = _pretty_status_desc(detail)
            print(f"  [{mark}] ({role}) {name}: {pretty_detail}")
    if revoked_accounts:
        print("警告: 以下账号 Session 已失效，需重新登录后执行 add 重建备份:")
        for name in revoked_accounts:
            print(f"  ! {name}  ->  python3 codex_mgr.py switch {name}")
    print(f"前台客户端: 未关闭、未切换 (前台账号 '{current_active}' 使用中)")
    print(sep)


def _check_token_status(profile_name, info, revoked_accounts):
    """检测账号 session 是否被服务器撤销；失效时仅记入列表，汇总区统一打印。"""
    if (info or {}).get("status") == "Token Expired":
        if profile_name not in revoked_accounts:
            revoked_accounts.append(profile_name)


def cmd_daemon():
    import signal

    # 标准 Unix Daemon 化：创建新会话，彻底脱离控制终端
    try:
        os.setsid()
    except OSError:
        pass  # 已是会话 leader，忽略

    def _sigterm_handler(signum, frame):
        print("守护进程收到停止信号，正在退出...")
        sys.exit(0)
    signal.signal(signal.SIGTERM, _sigterm_handler)

    interval_sec = 1800
    interval_min = interval_sec // 60
    cycle = 0

    print("=" * 60)
    print("Codex 保活守护进程已启动")
    print(f"  PID: {os.getpid()}")
    print(f"  周期: 每 {interval_min} 分钟执行一轮")
    print("  动作: 前台同步备份+额度校验 | 后台无头心跳保活")
    print("=" * 60)

    try:
        while True:
            cycle += 1
            print("")
            print(f">>> 第 {cycle} 轮")
            cmd_wakeup()
            next_at = datetime.datetime.now() + datetime.timedelta(seconds=interval_sec)
            next_str = next_at.strftime("%Y-%m-%d %H:%M:%S")
            print(f"本轮结束，休眠 {interval_min} 分钟；预计下次: {next_str}")
            time.sleep(interval_sec)
    except (KeyboardInterrupt, SystemExit):
        print("守护进程已安全退出。")


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
  switch new        准备一个干净的“未登录”客户端环境以供登录并录入新账号
  del/remove <name> 永久删除指定账号 Profile 的本地备份和用量缓存
  wakeup            手动执行一次批量刷新保活与用量更新
  daemon            在当前终端启动常驻后台守护进程 (建议使用 wakeup_daemon.sh 启动)
""")

def _disable_ansi_colors():
    """写入日志文件时关闭 ANSI 颜色，避免出现 [92m 这类乱码。"""
    global GREEN, RED, YELLOW, CYAN, BOLD, RESET
    GREEN = RED = YELLOW = CYAN = BOLD = RESET = ""


def main():
    if len(sys.argv) < 2:
        print_help()
        sys.exit(1)

    cmd = sys.argv[1]

    # 守护进程日志 / 非 TTY 输出：去掉颜色码，并给非空行加时间戳
    if cmd in ["daemon", "wakeup"]:
        if cmd == "daemon" or not sys.stdout.isatty():
            _disable_ansi_colors()
        import builtins
        _orig_print = builtins.print

        def timestamped_print(*args, **kwargs):
            # 空行保持空行，方便分块阅读
            if not args or (len(args) == 1 and str(args[0]).strip() == ""):
                _orig_print(*args, **kwargs)
                return
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            first_arg = str(args[0])
            if first_arg.startswith("\n"):
                body = first_arg[1:]
                if body.strip() == "":
                    _orig_print(*args, **kwargs)
                    return
                new_args = (f"\n[{now_str}] {body}",) + args[1:]
            else:
                new_args = (f"[{now_str}] {first_arg}",) + args[1:]
            _orig_print(*new_args, **kwargs)

        builtins.print = timestamped_print

    if cmd == "list":
        refresh = False
        target_profile = None
        for arg in sys.argv[2:]:
            if arg in ["--refresh", "-r"]:
                refresh = True
            else:
                target_profile = arg
        cmd_list(refresh, target_profile)
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
