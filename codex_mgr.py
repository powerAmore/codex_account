#!/usr/bin/env python3
import os
import sys
import json
import base64
import contextlib
import fcntl
import re
import shutil
import signal
import subprocess
import tempfile
import time
import datetime
import random
import unicodedata
import urllib.request
import urllib.error

# 安装缺失的 websocket-client 依赖
try:
    import websocket
    HAS_WEBSOCKET = True
except ImportError:
    HAS_WEBSOCKET = False

# 配置路径
APP_SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/Codex")
BACKUP_PREFIX = os.path.expanduser("~/Library/Application Support/Codex_Profile")
ACTIVE_FILE = os.path.expanduser("~/Library/Application Support/.active_codex_profile")
CODEX_HOME = os.path.expanduser("~/.codex")
AUTH_FILE = os.path.join(CODEX_HOME, "auth.json")
USAGE_CACHE_FILE = os.path.join(CODEX_HOME, "accounts_usage.json")
OPERATION_LOCK_FILE = os.path.join(CODEX_HOME, "codex_mgr_operation.lock")
DAEMON_LOCK_FILE = os.path.join(CODEX_HOME, "codex_mgr_daemon.lock")
DAEMON_PID_FILE = os.path.join(CODEX_HOME, "codex_mgr_daemon.pid")
CAFFEINATE_PID_FILE = os.path.join(CODEX_HOME, "codex_mgr_caffeinate.pid")
CODEX_CLI_PATH = "/Applications/ChatGPT.app/Contents/Resources/codex"

_DAEMON_LOCK_FD = None

# 本机使用 TUN 时，流量已经在网络层接管；再次读取 HTTP(S)_PROXY 会形成
# 应用层二次代理。需要传统 HTTP/SOCKS 代理时可显式设置为 env。
NETWORK_MODE_ENV = "CODEX_MGR_NETWORK_MODE"
DEFAULT_NETWORK_MODE = "tun"
DAEMON_INTERVAL_ENV = "CODEX_MGR_DAEMON_INTERVAL_MINUTES"
DEFAULT_DAEMON_INTERVAL_MINUTES = 120
OAUTH_REFRESH_MARGIN_SECONDS = 48 * 3600

# 颜色控制
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
CYAN = '\033[96m'
BOLD = '\033[1m'
RESET = '\033[0m'


def get_network_mode():
    """返回网络模式：tun 忽略应用层代理；env 显式使用 HTTP(S)_PROXY。"""
    mode = os.environ.get(NETWORK_MODE_ENV, DEFAULT_NETWORK_MODE).strip().lower()
    if mode not in ("tun", "env"):
        return DEFAULT_NETWORK_MODE
    return mode


def _url_opener_for_network_mode():
    if get_network_mode() == "env":
        return urllib.request.build_opener()
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _browser_proxy_args():
    """TUN 由系统路由接管；env 模式才让 Chromium 读取系统/环境代理。"""
    if get_network_mode() == "env":
        return []
    return ["--no-proxy-server"]


@contextlib.contextmanager
def operation_lock(timeout=60):
    """跨进程串行化 switch/refresh/wakeup，防止 Profile 与缓存相互覆盖。"""
    os.makedirs(CODEX_HOME, exist_ok=True)
    lock_file = open(OPERATION_LOCK_FILE, "a+")
    deadline = time.monotonic() + max(0, timeout)
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.2)
        yield acquired
    finally:
        if acquired:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def _atomic_write_text(path, text, mode=0o600):
    """同目录临时文件 + fsync + replace，避免崩溃留下半截文件。"""
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as temp_file:
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)

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
        _atomic_write_text(ACTIVE_FILE, name, mode=0o600)
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


def _auth_account_id(auth_data):
    return str(((auth_data or {}).get("tokens") or {}).get("account_id") or "")


def _auth_generation(auth_data):
    """返回凭据的新旧代际；用于防止旧快照覆盖已轮换的新凭据。"""
    values = []
    for token_name in ("access_token", "id_token"):
        payload = decode_jwt_payload(((auth_data or {}).get("tokens") or {}).get(token_name, ""))
        if isinstance(payload, dict):
            for field in ("iat", "exp"):
                try:
                    values.append(float(payload.get(field) or 0))
                except (TypeError, ValueError):
                    pass
    last_refresh = str((auth_data or {}).get("last_refresh") or "").strip()
    if last_refresh:
        try:
            parsed = datetime.datetime.fromisoformat(last_refresh.replace("Z", "+00:00"))
            values.append(parsed.timestamp())
        except (TypeError, ValueError):
            pass
    return max(values or [0])


def _read_auth_json(path):
    with open(path, "r") as auth_file:
        data = json.load(auth_file)
    if not isinstance(data, dict):
        raise ValueError("auth.json root must be an object")
    return data


def _sync_auth_snapshot(src, dst):
    """同账号凭据只向前同步。返回 (ok, action)。"""
    source = _read_auth_json(src)
    source_account = _auth_account_id(source)
    if os.path.exists(dst):
        target = _read_auth_json(dst)
        target_account = _auth_account_id(target)
        if source_account and target_account and source_account != target_account:
            return False, "identity-mismatch"
        if _auth_generation(target) > _auth_generation(source):
            return True, "destination-newer"
    _atomic_copy_file(src, dst, mode=0o600)
    return True, "copied"


def _access_token_needs_refresh(auth_data, now=None, margin=OAUTH_REFRESH_MARGIN_SECONDS):
    token = ((auth_data or {}).get("tokens") or {}).get("access_token", "")
    payload = decode_jwt_payload(token)
    try:
        expires_at = float((payload or {}).get("exp") or 0)
    except (TypeError, ValueError):
        return True
    if not expires_at:
        return True
    return expires_at - (time.time() if now is None else now) <= margin

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


def _atomic_copy_file(src, dst, mode=None):
    parent = os.path.dirname(dst)
    os.makedirs(parent, exist_ok=True)
    fd, staging = tempfile.mkstemp(prefix=f".{os.path.basename(dst)}.", dir=parent)
    os.close(fd)
    try:
        shutil.copy2(src, staging)
        if mode is not None:
            os.chmod(staging, mode)
        os.replace(staging, dst)
    finally:
        if os.path.exists(staging):
            os.unlink(staging)


def _snapshot_sqlite_database(src, dst):
    """使用 SQLite backup API 获取运行中数据库的一致快照。"""
    import sqlite3

    parent = os.path.dirname(dst)
    os.makedirs(parent, exist_ok=True)
    fd, staging = tempfile.mkstemp(prefix=f".{os.path.basename(dst)}.", dir=parent)
    os.close(fd)
    try:
        source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=5)
        target = sqlite3.connect(staging)
        source.backup(target)
        target.close()
        source.close()
        check = sqlite3.connect(f"file:{staging}?mode=ro", uri=True)
        integrity = check.execute("PRAGMA quick_check").fetchone()[0]
        check.close()
        if integrity != "ok":
            raise RuntimeError(f"SQLite snapshot integrity check failed: {integrity}")
        os.chmod(staging, 0o600)
        os.replace(staging, dst)
    finally:
        if os.path.exists(staging):
            os.unlink(staging)


def _replace_directory_copy(src, dst):
    """完整复制成功后再切换目录，并保留失败回滚路径。"""
    parent = os.path.dirname(dst)
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=f".{os.path.basename(dst)}.staging.", dir=parent)
    previous = dst + ".previous"
    moved_old = False
    try:
        shutil.copytree(src, staging, dirs_exist_ok=True)
        if os.path.exists(previous):
            shutil.rmtree(previous)
        if os.path.exists(dst):
            os.replace(dst, previous)
            moved_old = True
        os.replace(staging, dst)
        staging = None
    except Exception:
        if moved_old and not os.path.exists(dst) and os.path.exists(previous):
            os.replace(previous, dst)
        raise
    finally:
        if staging and os.path.exists(staging):
            shutil.rmtree(staging, ignore_errors=True)


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

    # active 标记可能因手工登录/崩溃而滞后。身份不一致时必须在复制 Cookie 前中止，
    # 否则守护进程会把 A 账号的登录态覆盖进 B Profile。
    backup_auth = os.path.join(backup_dir, "auth.json")
    if os.path.exists(AUTH_FILE) and os.path.exists(backup_auth):
        try:
            live_account = _auth_account_id(_read_auth_json(AUTH_FILE))
            saved_account = _auth_account_id(_read_auth_json(backup_auth))
            if live_account and saved_account and live_account != saved_account:
                if not quiet:
                    print(f"[{RED}ERROR{RESET}] 活跃账号身份与 Profile '{profile_name}' 不一致，已拒绝覆盖备份。")
                return False
        except Exception as e:
            if not quiet:
                print(f"[{RED}ERROR{RESET}] 校验 auth.json 身份失败: {e}")
            return False

    # 备份 App Support 核心文件
    app_support_backup = os.path.join(backup_dir, "app_support", "Default")
    os.makedirs(app_support_backup, exist_ok=True)

    default_dir = os.path.join(APP_SUPPORT_DIR, "Default")
    if os.path.exists(default_dir):
        if not quiet:
            print(f"正在增量备份活跃账号 Cookies & 本地存储...")
        # Cookies 是运行中的 SQLite：使用在线 backup API，禁止直接复制半写入文件。
        cookies_src = os.path.join(default_dir, "Cookies")
        if os.path.exists(cookies_src):
            try:
                _snapshot_sqlite_database(
                    cookies_src,
                    os.path.join(app_support_backup, "Cookies"),
                )
                for sidecar in ("Cookies-journal", "Cookies-wal", "Cookies-shm"):
                    stale_sidecar = os.path.join(app_support_backup, sidecar)
                    if os.path.exists(stale_sidecar):
                        os.remove(stale_sidecar)
            except Exception as e:
                if not quiet:
                    print(f"[{RED}ERROR{RESET}] Cookies 一致性快照失败: {e}")
                success = False

        # quiet=True 表示 App 正在运行的守护同步；避免复制正在写入的 LevelDB。
        if not quiet:
            for folder in ["Local Storage", "Session Storage"]:
                src_folder = os.path.join(default_dir, folder)
                dst_folder = os.path.join(app_support_backup, folder)
                if os.path.exists(src_folder):
                    try:
                        _replace_directory_copy(src_folder, dst_folder)
                    except Exception as e:
                        print(f"[{RED}ERROR{RESET}] 备份 '{folder}' 失败: {e}")
                        success = False

    # 备份 auth.json
    if os.path.exists(AUTH_FILE):
        try:
            auth_ok, auth_action = _sync_auth_snapshot(AUTH_FILE, backup_auth)
            if not auth_ok:
                raise RuntimeError(auth_action)
            if auth_action == "destination-newer" and not quiet:
                print("检测到 Profile 中的凭据更新，已保留较新版本，避免旧令牌回滚。")
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
        # 只还原一致性快照；上一个账号遗留的 SQLite WAL/SHM 必须清除。
        cookies_src = os.path.join(app_support_backup, "Cookies")
        cookies_dst = os.path.join(default_dir, "Cookies")
        if os.path.exists(cookies_src):
            _atomic_copy_file(cookies_src, cookies_dst, mode=0o600)
        elif os.path.exists(cookies_dst):
            os.remove(cookies_dst)
        for sidecar in ("Cookies-journal", "Cookies-wal", "Cookies-shm"):
            stale_sidecar = os.path.join(default_dir, sidecar)
            if os.path.exists(stale_sidecar):
                os.remove(stale_sidecar)
                    
        # 还原 Local Storage / Session Storage
        for folder in ["Local Storage", "Session Storage"]:
            src_folder = os.path.join(app_support_backup, folder)
            dst_folder = os.path.join(default_dir, folder)
            if os.path.exists(src_folder):
                _replace_directory_copy(src_folder, dst_folder)
            elif os.path.exists(dst_folder):
                shutil.rmtree(dst_folder)
                
    # 还原 auth.json
    backup_auth = os.path.join(backup_dir, "auth.json")
    if os.path.exists(backup_auth):
        os.makedirs(CODEX_HOME, exist_ok=True)
        _atomic_copy_file(backup_auth, AUTH_FILE, mode=0o600)
    elif os.path.exists(AUTH_FILE):
        try:
            os.remove(AUTH_FILE)
        except Exception:
            pass

    set_current_active(profile_name)
    return True

def cmd_add(profile_name=None):
    # ``switch new`` 的 new 是操作关键字，不是建议作为 Profile 别名。
    # 容忍用户顺手输入 ``add new``，仍按登录凭据自动命名。
    if str(profile_name or "").strip().lower() in ("new", "--new"):
        print("提示: add new 会按当前登录账号自动命名，不会创建名为 'new' 的 Profile。")
        profile_name = None

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
        if not backup_profile(current):
            print(f"{RED}[安全中止] 当前账号备份失败，已取消新增 Profile。{RESET}")
            return

    # 5. 创建新 Profile 目录并备份当前数据
    print(f"正在将当前登录态保存为新 Profile: '{profile_name}'...")
    os.makedirs(backup_dir, exist_ok=True)
    if not backup_profile(profile_name):
        print(f"{RED}[安全中止] 新 Profile 备份失败，未更新活跃账号标记。{RESET}")
        return
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
        if not backup_profile(current):
            print(f"{RED}[安全中止] 当前账号备份失败，已取消切换，避免登录态丢失或串号。{RESET}")
            return
        
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


def _is_valid_profile_name(name):
    valid_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.@")
    return bool(name) and all(c in valid_chars for c in name)


def cmd_rename(source_profile, target_profile):
    """原子重命名本地 Profile、活跃标记与用量缓存，不触碰服务器凭据。"""
    source_profile = str(source_profile or "").strip()
    target_profile = str(target_profile or "").strip()
    if not _is_valid_profile_name(target_profile):
        print(f"{RED}错误: 新 Profile 名称只允许英文字母、数字、下划线、连字符、点和 @ 符号。{RESET}")
        return False
    if source_profile == target_profile:
        print("提示: 新旧 Profile 名称相同，无需重命名。")
        return True

    source_dir = f"{BACKUP_PREFIX}_{source_profile}"
    target_dir = f"{BACKUP_PREFIX}_{target_profile}"
    if not os.path.isdir(source_dir):
        print(f"{RED}错误: Profile '{source_profile}' 不存在。{RESET}")
        return False
    if os.path.exists(target_dir):
        print(f"{RED}错误: Profile '{target_profile}' 已存在，未执行重命名。{RESET}")
        return False

    try:
        os.replace(source_dir, target_dir)
        usage_cache = load_usage_cache()
        if source_profile in usage_cache:
            usage_cache[target_profile] = usage_cache.pop(source_profile)
            save_usage_cache(usage_cache)
        if get_current_active() == source_profile:
            set_current_active(target_profile)
    except Exception as exc:
        print(f"{RED}错误: 重命名 Profile 失败: {type(exc).__name__}: {exc}{RESET}")
        return False

    print(f"{GREEN}Profile 已重命名: '{source_profile}' → '{target_profile}'{RESET}")
    return True

def find_free_port():
    """动态获取一个当前空闲可用的 TCP 端口。"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

def _copy_browser_state(src_default, dst_default, replace=False):
    """复制查询所需的最小浏览器状态；仅在浏览器进程停止后调用。"""
    os.makedirs(dst_default, exist_ok=True)
    for item in ["Cookies", "Cookies-journal"]:
        src = os.path.join(src_default, item)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dst_default, item))

    for folder in ["Local Storage", "Session Storage"]:
        src_folder = os.path.join(src_default, folder)
        dst_folder = os.path.join(dst_default, folder)
        if not os.path.exists(src_folder):
            continue
        if replace and os.path.exists(dst_folder):
            shutil.rmtree(dst_folder)
        if not os.path.exists(dst_folder):
            shutil.copytree(src_folder, dst_folder)


def _validate_cookie_db(default_dir):
    """确认待提升的 Cookie 数据库完整且仍包含 ChatGPT/OpenAI Cookie。"""
    import sqlite3

    cookie_path = os.path.join(default_dir, "Cookies")
    if not os.path.exists(cookie_path) or os.path.getsize(cookie_path) == 0:
        return False
    try:
        connection = sqlite3.connect(f"file:{cookie_path}?mode=ro", uri=True)
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        relevant_count = connection.execute(
            "SELECT COUNT(*) FROM cookies "
            "WHERE host_key LIKE '%chatgpt%' OR host_key LIKE '%openai%'"
        ).fetchone()[0]
        connection.close()
        return integrity == "ok" and relevant_count > 0
    except Exception:
        return False


def _promote_browser_state(temp_default, backup_default):
    """经认证的浏览器状态才以两阶段目录切换提升，并保留上一代可回滚副本。"""
    parent = os.path.dirname(backup_default)
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".Default.staging.", dir=parent)
    previous = backup_default + ".previous"
    moved_old = False
    try:
        _copy_browser_state(temp_default, staging)
        if not _validate_cookie_db(staging):
            raise RuntimeError("Cookie snapshot validation failed")

        if os.path.exists(previous):
            shutil.rmtree(previous)
        if os.path.exists(backup_default):
            os.replace(backup_default, previous)
            moved_old = True
        os.replace(staging, backup_default)
        staging = None
        return True
    except Exception:
        if moved_old and not os.path.exists(backup_default) and os.path.exists(previous):
            os.replace(previous, backup_default)
        return False
    finally:
        if staging and os.path.exists(staging):
            shutil.rmtree(staging, ignore_errors=True)


def _terminate_browser_tree(proc, temp_user_data):
    """结束整个查询进程组，并清理会脱离父进程的 crashpad 辅助进程。"""
    if proc is not None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                pass

    # Chromium crashpad 可能双重 fork 后变成 PPID=1；唯一临时目录可安全限定清理范围。
    try:
        match = subprocess.run(
            ["pgrep", "-f", temp_user_data],
            capture_output=True,
            text=True,
            timeout=2,
        )
        helper_pids = [int(value) for value in match.stdout.split() if value.isdigit()]
        for helper_pid in helper_pids:
            if helper_pid != os.getpid():
                try:
                    os.kill(helper_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        if helper_pids:
            time.sleep(0.15)
        for helper_pid in helper_pids:
            if helper_pid != os.getpid():
                try:
                    os.kill(helper_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    except Exception:
        pass


def _cdp_request(ws, request_state, method, params=None, timeout=10):
    """发送一个 CDP 请求并只等待自己的响应，事件消息会被安全忽略。"""
    request_state[0] += 1
    req_id = request_state[0]
    payload = {"id": req_id, "method": method}
    if params is not None:
        payload["params"] = params
    ws.send(json.dumps(payload))

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"CDP Timeout: {method}")
        ws.settimeout(remaining)
        frame = ws.recv()
        if not frame:
            raise RuntimeError(f"CDP Connection Closed: {method}")
        response = json.loads(frame)
        if response.get("id") != req_id:
            continue
        if response.get("error"):
            error = response["error"]
            raise RuntimeError(f"CDP Error: {method}: {error.get('message', error)}")
        return response


def _wait_for_cdp_page(opener, port, proc, timeout=15):
    """轮询调试端口直至真正可用，避免固定 sleep 带来的启动竞争。"""
    deadline = time.monotonic() + timeout
    last_error = "debugger endpoint not ready"
    created_page = False

    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Browser Exited Early: code {proc.returncode}")
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/json",
                headers={"User-Agent": "codex_mgr/1.0"},
            )
            with opener.open(req, timeout=1) as response:
                targets = json.loads(response.read().decode("utf-8"))

            pages = [
                target for target in targets
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl")
            ]
            if pages:
                preferred = next(
                    (page for page in pages if "chatgpt.com" in page.get("url", "")),
                    pages[0],
                )
                return preferred["webSocketDebuggerUrl"]

            if not created_page:
                new_req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/json/new",
                    method="PUT",
                    headers={"User-Agent": "codex_mgr/1.0"},
                )
                with opener.open(new_req, timeout=1) as response:
                    page = json.loads(response.read().decode("utf-8"))
                created_page = True
                if page.get("webSocketDebuggerUrl"):
                    return page["webSocketDebuggerUrl"]
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.25)

    raise TimeoutError(f"Browser Startup Timeout: {last_error}")


def _wait_for_chatgpt_page(ws, request_state, timeout=25):
    """等待导航与重定向稳定，避免在旧 execution context 中注入 fetch。"""
    deadline = time.monotonic() + timeout
    stable_checks = 0
    last_href = ""

    while time.monotonic() < deadline:
        remaining = max(0.5, min(5, deadline - time.monotonic()))
        try:
            response = _cdp_request(
                ws,
                request_state,
                "Runtime.evaluate",
                {
                    "expression": "({href: location.href, origin: location.origin, readyState: document.readyState})",
                    "returnByValue": True,
                },
                timeout=remaining,
            )
            result = response.get("result", {})
            if result.get("exceptionDetails"):
                stable_checks = 0
            else:
                value = result.get("result", {}).get("value") or {}
                href = str(value.get("href", ""))
                ready_state = value.get("readyState")
                is_ready = (
                    value.get("origin") == "https://chatgpt.com"
                    and ready_state in ("interactive", "complete")
                )
                if is_ready and href == last_href:
                    stable_checks += 1
                    if stable_checks >= 2:
                        return
                else:
                    stable_checks = 1 if is_ready else 0
                last_href = href
        except (TimeoutError, RuntimeError, ValueError, json.JSONDecodeError):
            # 导航过程中 execution context 被销毁是正常瞬态，继续等新页面。
            stable_checks = 0
        time.sleep(0.4)

    raise TimeoutError(f"Page Load Timeout: last URL {last_href or 'unknown'}")


def _parse_quota_fetch_result(fetch_result):
    """把带 HTTP 状态的浏览器 fetch 结果转换成稳定、可区分的状态。"""
    if not isinstance(fetch_result, dict):
        return {"status": f"Parse Error: {str(fetch_result)[:100]}"}

    if fetch_result.get("kind") == "fetch_error":
        error_name = str(fetch_result.get("name", ""))
        message = str(fetch_result.get("message", "Unknown fetch error"))
        if error_name == "AbortError" or "abort" in message.lower():
            return {"status": "Fetch Timeout"}
        return {"status": f"Network Error: {message}"}

    if fetch_result.get("kind") != "http":
        return {"status": f"Parse Error: {str(fetch_result)[:100]}"}

    http_status = int(fetch_result.get("status") or 0)
    body = fetch_result.get("body")
    body_text = str(body).lower()
    content_type = str(fetch_result.get("content_type") or "").lower()
    html_response = "text/html" in content_type or (
        isinstance(body, str) and body.lstrip().lower().startswith(("<!doctype html", "<html"))
    )
    challenge_response = html_response and (
        "cloudflare" in body_text or "challenge" in body_text or http_status in (401, 403)
    )

    # Cloudflare/代理边缘有时用 401 返回 HTML challenge。只有结构化 JSON 401
    # 才能证明 Bearer 被业务服务拒绝，不能再把所有 401 都报成“账号被踢”。
    if challenge_response:
        return {"status": "Blocked by Cloudflare", "http_status": http_status}
    if http_status == 401:
        if isinstance(body, dict):
            return {
                "status": "Token Rejected",
                "http_status": http_status,
                # 只有 JSON 401 才是两端点共识判断可用的强证据；其他包含
                # token 字样的响应可能来自网关、灰度或非标准错误页。
                "structured_401": True,
                "auth_failure_confirmed": True,
            }
        return {"status": "Auth Check Inconclusive: HTTP 401 non-JSON", "http_status": http_status}
    if http_status == 403:
        return {"status": "Blocked by Cloudflare"}

    if isinstance(body, dict):
        if "rate_limit" in body:
            result = {"status": "OK", "rate_limit": body["rate_limit"]}
            for identity_key in ("account_id", "email", "user_id", "plan_type"):
                if body.get(identity_key):
                    result[identity_key] = body[identity_key]
            return result
        if "error" in body:
            error_obj = body.get("error")
            if isinstance(error_obj, dict):
                error_code = str(error_obj.get("code", "")).lower()
                error_message = error_obj.get("message", str(error_obj))
            else:
                error_code = str(error_obj).lower()
                error_message = str(error_obj)
            if any(word in error_code for word in ("token", "invalid", "unauthorized")):
                return {"status": "Token Rejected", "auth_failure_confirmed": True}
            return {"status": f"API Error: HTTP {http_status}: {error_message}"}
        if "detail" in body:
            if "unauthorized" in body_text:
                return {"status": "Token Rejected", "auth_failure_confirmed": True}
            return {"status": f"API Error: HTTP {http_status}: {body.get('detail')}"}

    if http_status >= 400:
        return {"status": f"API Error: HTTP {http_status}: {str(body)[:100]}"}
    if "challenge" in body_text or "cloudflare" in body_text:
        return {"status": "Blocked by Cloudflare"}
    if any(word in body_text for word in ("unauthorized", "token is missing", "token_invalidated")):
        return {"status": "Token Rejected", "auth_failure_confirmed": True}
    return {"status": f"Parse Error: HTTP {http_status}: {str(body)[:100]}"}


def _network_failure_status(exc):
    """把底层网络异常收成稳定状态；SSL 握手超时单独标出便于区分节点问题。"""
    text = str(exc)
    lower = text.lower()
    if "handshake operation timed out" in lower or "handshake timed out" in lower:
        return "Fetch Timeout: SSL handshake"
    if "timed out" in lower or isinstance(exc, TimeoutError):
        return "Fetch Timeout"
    return f"Network Error: {type(exc).__name__}: {exc}"


def _query_quota_with_token(access_token, timeout=15):
    """用 Bearer Token 查询额度，并在两个官方兼容路径间回退。

    ChatGPT/Codex 后端在不同版本会在 ``codex/usage`` 与 ``wham/usage``
    之间切换。两者都返回同一份 rate_limit；单一路径被边缘节点挑战时，
    立即尝试另一路径，避免把一次 Cloudflare 403 当成账号失效。
    """
    if not access_token:
        return {"status": "No Access Token"}

    endpoints = (
        "https://chatgpt.com/backend-api/codex/usage",
        "https://chatgpt.com/backend-api/wham/usage",
    )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "codex/0.146.0",
    }
    opener = _url_opener_for_network_mode()
    last_result = {"status": "Unknown"}
    tried = []
    endpoint_statuses = []
    endpoint_results = []

    for endpoint in endpoints:
        endpoint_name = endpoint.split("/backend-api/", 1)[-1]
        tried.append(endpoint_name)
        request = urllib.request.Request(endpoint, headers=headers)
        try:
            with opener.open(request, timeout=timeout) as response:
                raw_body = response.read().decode("utf-8", errors="replace")
                try:
                    body = json.loads(raw_body)
                except json.JSONDecodeError:
                    body = raw_body
                result = _parse_quota_fetch_result({
                    "kind": "http",
                    "status": response.status,
                    "body": body,
                    "content_type": getattr(response, "headers", {}).get("content-type", ""),
                })
        except urllib.error.HTTPError as exc:
            raw_body = exc.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw_body)
            except json.JSONDecodeError:
                body = raw_body
            result = _parse_quota_fetch_result({
                "kind": "http",
                "status": exc.code,
                "body": body,
                "content_type": exc.headers.get("content-type", ""),
            })
            # cf-ray 只用于诊断边缘节点，不记录响应正文或任何凭证。
            cf_ray = exc.headers.get("cf-ray")
            if cf_ray:
                result["cf_ray"] = cf_ray
        except (TimeoutError, OSError) as exc:
            result = {"status": _network_failure_status(exc)}
        except Exception as exc:
            result = {"status": _network_failure_status(exc)}

        result["source"] = "bearer"
        result["quota_endpoint"] = endpoint_name
        endpoint_statuses.append(str(result.get("status", "Unknown")))
        endpoint_results.append(result)
        if result.get("status") == "OK":
            if len(tried) > 1:
                result["quota_fallback_used"] = True
                result["quota_endpoints_tried"] = tried
            return result

        last_result = result
        # 两条兼容路径都返回结构化 401 才形成凭据拒绝共识；单一路径 401
        # 也要检查另一条，避免把端点灰度/边缘异常误报成账号失效。
        status = str(result.get("status", ""))
        retryable = (
            status in ("Blocked by Cloudflare", "Fetch Timeout", "Token Rejected")
            or status.startswith("Fetch Timeout:")
            or status.startswith("Network Error")
            or status.startswith("API Error: HTTP 429")
            or status.startswith("API Error: HTTP 5")
        )
        if not retryable:
            break

    last_result["source"] = "bearer"
    last_result["quota_endpoints_tried"] = tried
    last_result["quota_endpoint_statuses"] = endpoint_statuses
    structured_401_count = sum(
        status == "Token Rejected" and result.get("structured_401")
        for status, result in zip(endpoint_statuses, endpoint_results)
    )
    if structured_401_count and structured_401_count != len(endpoint_statuses):
        return {
            "status": "Auth Check Inconclusive: quota endpoints disagree",
            "source": "bearer",
            "quota_endpoints_tried": tried,
            "quota_endpoint_statuses": endpoint_statuses,
            "auth_failure_confirmed": False,
        }
    if structured_401_count == len(endpoint_statuses) and structured_401_count:
        last_result["auth_failure_confirmed"] = True
    return last_result


def _parse_subscription_fetch_result(fetch_result):
    """解析 subscriptions 接口，提取 Plan / 到期日等订阅元数据。"""
    if not isinstance(fetch_result, dict):
        return {"status": f"Parse Error: {str(fetch_result)[:100]}"}

    if fetch_result.get("kind") == "fetch_error":
        error_name = str(fetch_result.get("name", ""))
        message = str(fetch_result.get("message", "Unknown fetch error"))
        if error_name == "AbortError" or "abort" in message.lower():
            return {"status": "Fetch Timeout"}
        return {"status": f"Network Error: {message}"}

    if fetch_result.get("kind") != "http":
        return {"status": f"Parse Error: {str(fetch_result)[:100]}"}

    http_status = int(fetch_result.get("status") or 0)
    body = fetch_result.get("body")

    if http_status == 401:
        return {"status": "Token Expired"}
    if http_status == 403:
        return {"status": "Blocked by Cloudflare"}
    if http_status >= 400:
        detail = body
        if isinstance(body, dict):
            detail = body.get("detail") or body.get("error") or body
        return {"status": f"API Error: HTTP {http_status}: {str(detail)[:100]}"}

    if not isinstance(body, dict):
        return {"status": f"Parse Error: HTTP {http_status}: {str(body)[:100]}"}

    plan_type = body.get("plan_type")
    active_until = body.get("active_until")
    if not plan_type and not active_until:
        return {"status": f"Parse Error: missing subscription fields: {str(body)[:100]}"}

    result = {"status": "OK", "source": "subscriptions"}
    if plan_type:
        result["plan_type"] = plan_type
    if active_until:
        result["subscription_until"] = active_until
    if body.get("active_start"):
        result["subscription_active_start"] = body["active_start"]
    if "will_renew" in body:
        result["will_renew"] = bool(body.get("will_renew"))
    if "is_delinquent" in body:
        result["is_delinquent"] = bool(body.get("is_delinquent"))
    if body.get("grace_period_end_timestamp"):
        result["grace_period_end"] = body["grace_period_end_timestamp"]
    if body.get("billing_period"):
        result["billing_period"] = body["billing_period"]
    return result


def _query_subscription_with_token(access_token, account_id, timeout=15, max_attempts=2):
    """用 Bearer Token 查询 ChatGPT 订阅计划与到期日。"""
    if not access_token:
        return {"status": "No Access Token"}
    if not account_id:
        return {"status": "No Account ID"}

    url = f"https://chatgpt.com/backend-api/subscriptions?account_id={account_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "codex/0.146.0",
        "ChatGPT-Account-ID": str(account_id),
        "Referer": "https://chatgpt.com/",
        "Origin": "https://chatgpt.com",
    }
    opener = _url_opener_for_network_mode()
    last_result = {"status": "Unknown"}

    for attempt in range(max(1, max_attempts)):
        request = urllib.request.Request(url, headers=headers)
        try:
            with opener.open(request, timeout=timeout) as response:
                raw_body = response.read().decode("utf-8", errors="replace")
                try:
                    body = json.loads(raw_body)
                except json.JSONDecodeError:
                    body = raw_body
                result = _parse_subscription_fetch_result(
                    {"kind": "http", "status": response.status, "body": body}
                )
        except urllib.error.HTTPError as exc:
            raw_body = exc.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw_body)
            except json.JSONDecodeError:
                body = raw_body
            result = _parse_subscription_fetch_result(
                {"kind": "http", "status": exc.code, "body": body}
            )
            cf_ray = exc.headers.get("cf-ray")
            if cf_ray:
                result["cf_ray"] = cf_ray
        except (TimeoutError, OSError) as exc:
            result = {"status": _network_failure_status(exc)}
        except Exception as exc:
            result = {"status": _network_failure_status(exc)}

        last_result = result
        if result.get("status") == "OK":
            return result

        status = str(result.get("status", ""))
        retryable = (
            status in ("Blocked by Cloudflare", "Fetch Timeout")
            or status.startswith("Fetch Timeout:")
            or status.startswith("Network Error")
            or status.startswith("API Error: HTTP 429")
            or status.startswith("API Error: HTTP 5")
        )
        if not retryable or attempt + 1 >= max_attempts:
            break
        time.sleep(0.4)

    return last_result


def _merge_subscription_into_quota_result(quota_result, subscription_result):
    """订阅查询失败不拖垮额度成功；成功则覆盖 Plan / Until。"""
    result = dict(quota_result)
    if not isinstance(subscription_result, dict):
        result["subscription_status"] = "Unknown"
        return result

    status = subscription_result.get("status", "Unknown")
    result["subscription_status"] = status
    if status != "OK":
        return result

    for key in (
        "plan_type",
        "subscription_until",
        "subscription_active_start",
        "will_renew",
        "is_delinquent",
        "grace_period_end",
        "billing_period",
    ):
        if key in subscription_result:
            result[key] = subscription_result[key]
    return result


def _date_only(value):
    """把 ISO 时间戳裁成 YYYY-MM-DD；无法解析时原样返回。"""
    if not value or value == "Unknown":
        return value
    text = str(value).strip()
    if "T" in text:
        text = text.split("T", 1)[0]
    return text[:10] if len(text) >= 10 and text[4] == "-" else text


def _mmdd(value):
    date = _date_only(value)
    if isinstance(date, str) and len(date) >= 10 and date[4] == "-":
        return date[5:10]
    return date


def _plan_and_until_from_auth_payload(payload):
    """从 id_token payload 提取 Plan / Until（本地兜底）。"""
    plan = "Unknown"
    until = "Unknown"
    if not isinstance(payload, dict):
        return plan, until
    auth_sec = payload.get("https://api.openai.com/auth", {}) or {}
    plan = str(auth_sec.get("chatgpt_plan_type", "free") or "free").upper()
    until_raw = auth_sec.get("chatgpt_subscription_active_until")
    if until_raw:
        until = _date_only(until_raw)
    return plan, until


def _format_subscription_until(limits):
    """格式化缓存中的订阅到期展示；欠费宽限期附在日期后。"""
    until = _date_only(limits.get("subscription_until") or "Unknown")
    if until == "Unknown":
        return until
    if limits.get("is_delinquent") and limits.get("grace_period_end"):
        return f"{until} 宽限{_mmdd(limits.get('grace_period_end'))}"
    if limits.get("will_renew") is False:
        return f"{until} 不续费"
    return until


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _display_width(text):
    """终端显示宽度：全角/宽字符计 2，忽略 ANSI 颜色码。"""
    width = 0
    for ch in _ANSI_RE.sub("", str(text)):
        if ch in ("\n", "\r"):
            continue
        east = unicodedata.east_asian_width(ch)
        if east in ("F", "W"):
            width += 2
        elif east == "A":
            # 终端对 ambiguous 宽字符通常按西文宽度渲染；表情等按 2 更稳。
            width += 2 if ord(ch) > 0xFF else 1
        else:
            width += 1
    return width


def _pad_display(text, width, align="<"):
    """按显示宽度填充，保证中英文混排时后续列对齐。"""
    text = "" if text is None else str(text)
    current = _display_width(text)
    if current > width:
        # 超宽时截断到可用宽度，避免把后面的列整体挤歪。
        kept = []
        used = 0
        for ch in text:
            if ch == "\033":
                # 保底：截断场景通常是纯文本字段，不做复杂 ANSI 截断。
                break
            step = _display_width(ch)
            if used + step > width - 1:
                break
            kept.append(ch)
            used += step
        text = "".join(kept) + "…"
        current = _display_width(text)
    pad = max(0, width - current)
    if align == ">":
        return (" " * pad) + text
    if align == "^":
        left = pad // 2
        return (" " * left) + text + (" " * (pad - left))
    return text + (" " * pad)


def _resolve_plan_and_until(auth_path, usage_data):
    """优先用 refresh 缓存的订阅元数据，否则回退本地 JWT。"""
    plan = "Unknown"
    until = "Unknown"
    if os.path.exists(auth_path):
        try:
            with open(auth_path, "r") as auth_file:
                auth_data = json.load(auth_file)
            id_token = (auth_data.get("tokens") or {}).get("id_token")
            if id_token:
                plan, until = _plan_and_until_from_auth_payload(decode_jwt_payload(id_token))
        except Exception:
            pass

    limits = (usage_data or {}).get("limits") or {}
    if limits.get("plan_type"):
        plan = str(limits["plan_type"]).upper()
    if limits.get("subscription_until"):
        until = _format_subscription_until(limits)
    return plan, until


def _validate_heartbeat_identity(heartbeat, expected_account_id, expected_email):
    """Cookie 心跳必须返回同一账号身份，防止跨 Profile 状态串号。"""
    actual_account_id = str(heartbeat.get("account_id") or "")
    actual_email = str(heartbeat.get("email") or "").lower()
    expected_account_id = str(expected_account_id or "")
    expected_email = str(expected_email or "").lower()

    if expected_account_id:
        if not actual_account_id:
            return False, "Cookie Identity Missing: account_id"
        if actual_account_id != expected_account_id:
            return False, "Cookie Identity Mismatch: account_id"
    if expected_email:
        if not actual_email:
            return False, "Cookie Identity Missing: email"
        if actual_email != expected_email:
            return False, "Cookie Identity Mismatch: email"
    return True, "OK"


def _oauth_probe_environment(temp_home):
    env = os.environ.copy()
    env["CODEX_HOME"] = temp_home
    if get_network_mode() == "tun":
        for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
        ):
            env.pop(key, None)
    return env


def _probe_codex_oauth(profile_name, timeout=35):
    """用官方 Codex 认证 WebSocket 验证/按需续签后台 Profile。"""
    backup_dir = f"{BACKUP_PREFIX}_{profile_name}"
    auth_path = os.path.join(backup_dir, "auth.json")
    if not os.path.exists(auth_path):
        return {"status": "No Auth Data", "source": "codex-doctor"}
    if not os.path.exists(CODEX_CLI_PATH):
        return {"status": "Codex CLI Missing", "source": "codex-doctor"}

    temp_home = tempfile.mkdtemp(prefix="codex_oauth_probe_")
    temp_auth = os.path.join(temp_home, "auth.json")
    try:
        _atomic_copy_file(auth_path, temp_auth, mode=0o600)
        original = _read_auth_json(auth_path)
        original_account = str((original.get("tokens") or {}).get("account_id") or "")

        completed = subprocess.run(
            [CODEX_CLI_PATH, "doctor", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_oauth_probe_environment(temp_home),
        )
        # doctor 的总体退出码可能被更新检查等无关项拉成非零；这里仅依据
        # auth.credentials 与 authenticated WebSocket 两项做认证判断。
        try:
            report = json.loads(completed.stdout)
        except json.JSONDecodeError:
            detail = (completed.stderr or completed.stdout or "unknown error").strip()
            detail = " ".join(detail.splitlines())
            return {
                "status": f"OAuth Probe Error: {detail[:160]}",
                "source": "codex-doctor",
            }
        checks = report.get("checks") or {}
        credentials = checks.get("auth.credentials") or {}
        websocket_check = checks.get("network.websocket_reachability") or {}
        if credentials.get("status") != "ok":
            detail = credentials.get("summary") or "credentials check failed"
            return {"status": f"OAuth Credentials Error: {detail}", "source": "codex-doctor"}

        # 官方 CLI 可能在 access token 临近/已经过期时轮换 refresh token。
        # 即使 WebSocket 诊断失败，也要先检查 CLI 是否已安全写入新凭据；旧逻辑
        # 在 warning 处提前返回，会丢掉已经完成的令牌轮换。
        refreshed = _read_auth_json(temp_auth)
        refreshed_account = str((refreshed.get("tokens") or {}).get("account_id") or "")
        if original_account and refreshed_account != original_account:
            return {
                "status": "OAuth Identity Mismatch: account_id",
                "source": "codex-doctor",
            }
        with open(auth_path, "rb") as original_file, open(temp_auth, "rb") as refreshed_file:
            changed = original_file.read() != refreshed_file.read()
        if changed:
            sync_ok, sync_action = _sync_auth_snapshot(temp_auth, auth_path)
            if not sync_ok:
                return {
                    "status": f"OAuth Credential Promotion Failed: {sync_action}",
                    "source": "codex-doctor",
                }
            changed = sync_action == "copied"

        if websocket_check.get("status") != "ok":
            detail = websocket_check.get("summary") or "authenticated websocket failed"
            return {
                "status": f"OAuth Network Error: {detail}",
                "source": "codex-doctor",
                "credentials_refreshed": changed,
            }

        return {
            "status": "OK",
            "source": "codex-doctor",
            "credentials_refreshed": changed,
        }
    except subprocess.TimeoutExpired:
        return {"status": "OAuth Probe Timeout", "source": "codex-doctor"}
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        return {
            "status": f"OAuth Probe Error: {type(exc).__name__}: {exc}",
            "source": "codex-doctor",
        }
    finally:
        shutil.rmtree(temp_home, ignore_errors=True)


def _combine_quota_and_oauth_result(direct_result, oauth_result):
    """合并额度与认证结果，避免把 HTTPS 已认证误报成掉登。"""
    result = dict(direct_result)
    oauth_status = oauth_result.get("status", "Unknown")

    # Bearer 额度接口已返回 rate_limit 时，认证已被 HTTPS 服务端确认。
    # WSS 诊断超时只反映当前 VPN/边缘节点的 WebSocket 可达性，不应覆盖它。
    if (
        direct_result.get("status") == "OK"
        and not _is_hard_auth_failure(oauth_status)
        and oauth_status != "OK"
    ):
        result["auth_status"] = "OK"
        result["auth_transport"] = "https"
        result["websocket_warning"] = oauth_status
    else:
        result["auth_status"] = oauth_status
        result["auth_transport"] = "websocket" if oauth_status == "OK" else None

    if oauth_result.get("credentials_refreshed"):
        result["credentials_refreshed"] = True
    return result


def _query_quota_once(profile_name, probe_oauth=True, include_subscription=True):
    """先做低成本 HTTPS 查询；仅在需要续签/恢复时运行 OAuth 探针。"""
    backup_dir = f"{BACKUP_PREFIX}_{profile_name}"
    auth_path = os.path.join(backup_dir, "auth.json")
    if not os.path.exists(auth_path):
        return {"status": "No Auth Data", "auth_status": "No Auth Data"}

    access_token = ""
    account_id = ""
    try:
        auth_data = _read_auth_json(auth_path)
        tokens = auth_data.get("tokens") or {}
        access_token = tokens.get("access_token", "")
        account_id = tokens.get("account_id", "")
    except Exception as exc:
        return {
            "status": f"Auth Read Error: {type(exc).__name__}: {exc}",
            "auth_status": "Auth Read Error",
        }

    direct_result = _query_quota_with_token(access_token)
    needs_oauth = probe_oauth and (
        direct_result.get("status") == "Token Rejected"
        or (direct_result.get("status") == "OK" and _access_token_needs_refresh(auth_data))
    )
    if needs_oauth:
        oauth_result = _probe_codex_oauth(profile_name)
        if oauth_result.get("credentials_refreshed"):
            # 续签成功后必须重新读取并验证新 access token，而不是继续使用旧值。
            auth_data = _read_auth_json(auth_path)
            tokens = auth_data.get("tokens") or {}
            access_token = tokens.get("access_token", "")
            account_id = tokens.get("account_id", "")
            direct_result = _query_quota_with_token(access_token)
    else:
        oauth_result = {
            "status": "OK",
            "source": "active-app" if not probe_oauth else "https",
            "probe_skipped": True,
        }
    combined = _combine_quota_and_oauth_result(direct_result, oauth_result)
    # 守护轮询不需要每次查询订阅；手动 refresh 才查询，降低请求频率。
    if include_subscription and direct_result.get("status") == "OK":
        subscription_result = _query_subscription_with_token(access_token, account_id)
    else:
        subscription_result = {"status": "Skipped"}
    return _merge_subscription_into_quota_result(combined, subscription_result)


def _is_transient_query_status(status):
    transient_prefixes = (
        "OAuth Probe Timeout",
        "OAuth Probe Error",
        "OAuth Network Error",
        "Browser Startup Timeout",
        "Browser Exited Early",
        "CDP Timeout",
        "CDP Connection Closed",
        "CDP Error",
        "Page Load Timeout",
        "Page Context Error",
        "Fetch Timeout",
        "Fetch Timeout:",
        "Network Error",
        "Runtime Error",
        "Parse Error",
        "Blocked by Cloudflare",
        "Auth Check Inconclusive",
        "Cookie Identity Missing",
        "State Promotion Failed",
        "API Error: HTTP 429",
        "API Error: HTTP 5",
    )
    return str(status).startswith(transient_prefixes)


def _is_hard_auth_failure(status):
    """只有凭证/身份问题才应覆盖已经成功的 Bearer 额度结果。"""
    return str(status or "").startswith((
        "No Auth",
        "Auth Read Error",
        "OAuth Credentials Error",
        "OAuth Identity Mismatch",
        "Codex CLI Missing",
    ))


def silent_query_quota(profile_name, max_attempts=2, probe_oauth=True, include_subscription=True):
    """额度和官方 OAuth 认证分别判定；任一瞬态失败都会重试。"""
    attempts = []
    for attempt in range(max(1, max_attempts)):
        try:
            info = _query_quota_once(
                profile_name,
                probe_oauth=probe_oauth,
                include_subscription=include_subscription,
            )
        except Exception as exc:
            info = {"status": f"Runtime Error: query setup: {type(exc).__name__}: {exc}"}
        status = info.get("status", "Unknown")
        auth_status = info.get("auth_status")
        attempt_desc = status
        if auth_status:
            attempt_desc += f"; oauth={auth_status}"
        attempts.append(attempt_desc)

        quota_ok = status == "OK"
        auth_ok = auth_status == "OK" or (quota_ok and not _is_hard_auth_failure(auth_status))
        hard_rejected = status == "Token Rejected" and _is_hard_auth_failure(auth_status)
        retryable = (
            not quota_ok
            and (
                status == "Token Rejected"
                or _is_transient_query_status(status)
                or (auth_status and _is_transient_query_status(auth_status))
            )
        )
        if (quota_ok and auth_ok) or hard_rejected or not retryable:
            break
        if attempt + 1 < max_attempts:
            time.sleep(0.8 * (attempt + 1))

    info["attempts"] = len(attempts)
    if len(attempts) > 1:
        info["attempt_history"] = attempts[:-1]
    if info.get("status") == "Token Rejected":
        info["auth_failure_confirmed"] = bool(
            info.get("auth_failure_confirmed")
            and (
                _is_hard_auth_failure(info.get("auth_status"))
                or (len(attempts) >= 2 and all(item.startswith("Token Rejected") for item in attempts))
            )
        )
    return info

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
        payload = json.dumps(cache, indent=2)
        _atomic_write_text(USAGE_CACHE_FILE, payload, mode=0o600)
    except Exception:
        pass


def _record_usage_result(cache, profile_name, info, timestamp=None):
    """成功结果替换缓存；瞬态失败只记告警，不抹掉最近一次成功额度。"""
    now = time.time() if timestamp is None else timestamp
    previous = cache.get(profile_name) or {}
    previous_limits = previous.get("limits") or {}

    if info.get("status") == "OK" or previous_limits.get("status") != "OK":
        new_limits = dict(info)
        # 额度成功但订阅现查失败时，保留上次订阅元数据，避免 Until 回退抖动。
        if info.get("status") == "OK" and info.get("subscription_status") != "OK":
            for key in (
                "subscription_until",
                "subscription_active_start",
                "will_renew",
                "is_delinquent",
                "grace_period_end",
                "billing_period",
            ):
                if key not in new_limits and key in previous_limits:
                    new_limits[key] = previous_limits[key]
            if "plan_type" not in new_limits and previous_limits.get("plan_type"):
                new_limits["plan_type"] = previous_limits["plan_type"]
        cache[profile_name] = {
            "limits": new_limits,
            "timestamp": now,
        }
        return

    preserved = dict(previous)
    preserved["last_error"] = {
        "limits": info,
        "timestamp": now,
    }
    cache[profile_name] = preserved

def _cmd_list_impl(refresh=False, target_profile=None):
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
        print("正在静默现查指定账号的限额、额度与订阅..." if target_profile else "正在静默现查所有账号的限额、额度与订阅 (HTTPS 优先，按需 OAuth，不打扰当前客户端)...")
        # 对每一个 profile 现查
        for i, p in enumerate(to_refresh):
            print(f"  [{i+1}/{len(to_refresh)}] 正在查询账号: {p} ...", end="", flush=True)
            info = silent_query_quota(p, probe_oauth=(p != current))
            _record_usage_result(usage_cache, p, info)
            auth_status = info.get("auth_status")
            if info.get("status") == "OK" and auth_status == "OK":
                if info.get("auth_transport") == "https" and info.get("websocket_warning"):
                    print(f" {GREEN}额度与 HTTPS 认证成功（WebSocket 探针超时）{RESET}")
                else:
                    print(f" {GREEN}额度与 OAuth 认证均成功{RESET}")
            elif info.get("status") == "OK":
                print(f" {YELLOW}额度成功，OAuth 告警: {auth_status or 'Unknown'}{RESET}")
            else:
                print(f" {YELLOW}{info.get('status', 'Unknown')}{RESET}")
        save_usage_cache(usage_cache)
        print("")

    # 5. 打印表格 (按终端显示宽度对齐，兼容中英文混排)
    print(f"\n{BOLD}{CYAN}=== ChatGPT/Codex 账号管理列表 ==={RESET}")
    
    col_active = 10
    col_profile = 25
    col_email = 45
    col_plan = 8
    # 最长示例: "2026-08-01 宽限08-04" 显示宽度 20
    col_until = 22
    table_width = col_active + col_profile + col_email + col_plan + col_until + 45
    
    header = (
        _pad_display("Active", col_active)
        + _pad_display("Profile Name", col_profile)
        + _pad_display("Email", col_email)
        + _pad_display("Plan", col_plan)
        + _pad_display("Subscription Until", col_until)
        + "Quota / Limit Info"
    )
    print("-" * table_width)
    print(f"{BOLD}{header}{RESET}")
    print("-" * table_width)
    
    for p in profiles:
        if target_profile and p != target_profile:
            continue
        backup_dir = f"{BACKUP_PREFIX}_{p}"
        auth_path = os.path.join(backup_dir, "auth.json")
        
        # 读取用量缓存（含 refresh 写入的 Plan / Subscription Until）
        usage_data = usage_cache.get(p, {})
        email = "Unknown"
        plan, until = _resolve_plan_and_until(auth_path, usage_data)
        is_active = (p == current)
        
        # 本地解析 JWT 邮箱（Plan/Until 已由 _resolve_plan_and_until 处理）
        if os.path.exists(auth_path):
            try:
                with open(auth_path, 'r') as f:
                    auth_data = json.load(f)
                id_token = auth_data.get("tokens", {}).get("id_token")
                if id_token:
                    payload = decode_jwt_payload(id_token)
                    if payload:
                        email = payload.get("email", "Unknown")
            except Exception:
                pass
                
        limits = usage_data.get("limits", {})
        limits_status = limits.get("status", "No Data")
        
        # 格式化用量限制展示
        quota_str = ""
        if limits_status in ("Token Expired", "Token Rejected"):
            quota_str = f"{RED}{limits_status} ❌{RESET}"
        elif limits_status == "Network Timeout":
            quota_str = f"{YELLOW}Network Timeout ⚠️{RESET}"
        elif limits_status == "Blocked by Cloudflare":
            quota_str = f"{YELLOW}CF Check Failed ⚠️{RESET}"
        elif limits_status == "No Data":
            quota_str = f"{YELLOW}No Data (请使用 --refresh 现查){RESET}"
        elif limits_status == "OK" and "rate_limit" in limits:
            rl = limits.get("rate_limit") or {}
            quota_str = format_rate_limit(rl, use_color=True)
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

        # 若最近刷新失败但仍有成功额度，保留额度并附加告警，不制造“整行超时”。
        last_error = usage_data.get("last_error") or {}
        last_error_limits = last_error.get("limits") or {}
        last_error_status = last_error_limits.get("status")
        if last_error_status:
            error_ts = last_error.get("timestamp")
            error_age = "刚刚"
            if error_ts:
                error_mins = int((time.time() - error_ts) / 60)
                error_age = "刚刚" if error_mins == 0 else f"{error_mins}分钟前"
            quota_str += f" | {YELLOW}刷新失败: {last_error_status} ({error_age}){RESET}"

        auth_status = limits.get("auth_status")
        if auth_status is None:
            auth_status = limits.get("heartbeat_status")  # 兼容旧缓存
        if auth_status and auth_status != "OK":
            quota_str += f" | {YELLOW}OAuth 告警: {auth_status}{RESET}"

        subscription_status = limits.get("subscription_status")
        if subscription_status and subscription_status != "OK" and limits_status == "OK":
            quota_str += f" | {YELLOW}订阅现查失败: {subscription_status}{RESET}"
                
        # 按显示宽度填充前几列；Quota 放最后，可含 ANSI 颜色
        active_val = _pad_display("* ACTIVE" if is_active else "", col_active)
        if is_active:
            active_val = active_val.replace("* ACTIVE", f"{GREEN}* ACTIVE{RESET}")
        line = (
            active_val
            + _pad_display(p, col_profile)
            + _pad_display(email, col_email)
            + _pad_display(plan, col_plan)
            + _pad_display(until, col_until)
            + quota_str
        )
        print(line)
        
    print("-" * table_width)
    if not refresh:
        print(f"提示: 以上额度与订阅基于缓存展示。运行 {BOLD}python3 codex_mgr.py list --refresh{RESET} 可静默现查最新额度与订阅。")
    print(f"提示: 后台守护服务默认约每 2 小时校验并刷新用量缓存（含随机抖动）。")


def cmd_list(refresh=False, target_profile=None):
    if not refresh:
        return _cmd_list_impl(refresh=False, target_profile=target_profile)
    with operation_lock(timeout=60) as acquired:
        if not acquired:
            print(f"{YELLOW}已有账号切换/刷新/保活操作正在运行，请稍后重试。{RESET}")
            return
        return _cmd_list_impl(refresh=True, target_profile=target_profile)

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

def format_rate_limit(rl, use_color=True):
    """把 rate_limit 字典格式化为适合单行或日志打印的额度用量文本。"""
    if not isinstance(rl, dict) or not rl:
        return "OK"
    parts = []
    for key in ["primary_window", "secondary_window"]:
        win = rl.get(key)
        if not isinstance(win, dict) or not win:
            continue
        
        # 计算剩余百分比 (剩余 = 100 - 已用)
        used_pct = win.get("used_percent", 0)
        pct_left = 100 - used_pct
        
        if use_color:
            pct_str = f"{RED}{pct_left}%{RESET}" if pct_left <= 15 else f"{GREEN}{pct_left}%{RESET}"
        else:
            pct_str = f"{pct_left}%"
            
        # 重置时间
        r_str = ""
        r_ts = win.get("reset_at")
        window_seconds = win.get("limit_window_seconds", 0)
        
        if r_ts:
            try:
                dt = datetime.datetime.fromtimestamp(r_ts)
                # 精确显示到月日、时:分:秒 (按照系统本地时区)
                r_str = " " + dt.strftime("%b %d %H:%M:%S")
            except Exception:
                pass
                
        win_name = format_window_name(window_seconds)
        parts.append(f"{win_name}: {pct_str}{r_str}")
        
    return " | ".join(parts) if parts else "OK"

def _pretty_status_desc(status):
    """把接口底层的英文/异常状态翻译并附带人机友好的详细解释。"""
    if not status:
        return "Unknown"
    if status == "OK":
        return "OK"
    if status.startswith("OK ("):
        return status
    if status in ("Token Expired", "Token Rejected"):
        return f"{status} (凭据被服务端拒绝；连续确认后才判定需重新登录)"
    if status.startswith("Auth Check Inconclusive"):
        return (
            "Auth Check Inconclusive (两个兼容额度端点的结果不一致；"
            "可能是当前节点或 Cloudflare，未判定登录失效)"
        )
    if status == "Network Timeout":
        return "Network Timeout (接口请求超时，请检查您的代理/节点速度)"
    if status == "Fetch Timeout: SSL handshake":
        return "Fetch Timeout: SSL handshake (到 chatgpt.com 的 TLS 握手失败，多为当前 VPN/节点路由问题)"
    if status == "Fetch Timeout" or status.startswith("Fetch Timeout:"):
        return f"{status} (用量接口超时未返回，请检查代理/节点)"
    if status.startswith("Browser Startup Timeout"):
        return f"浏览器启动超时 ({status})"
    if status.startswith("Page Load Timeout"):
        return f"页面加载超时 ({status})"
    if status.startswith("CDP Timeout"):
        return f"CDP 通信超时 ({status})"
    if status.startswith("Network Error"):
        return f"浏览器网络错误 ({status})"
    if status.startswith("Page Context Error"):
        return f"页面上下文失效 ({status})"
    if status.startswith("Runtime Error"):
        return f"后台认证查询运行错误 ({status})"
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
    if pretty_detail.startswith("OK ("):
        return pretty_detail
    return f"{mark}  {pretty_detail}"

def _is_query_ok(info):
    """额度成功且没有硬凭证/身份错误，即可作为可用结果。"""
    info = info or {}
    return info.get("status") == "OK" and not _is_hard_auth_failure(info.get("auth_status"))


def _cmd_wakeup_impl():
    """
    执行一轮账号保活与额度刷新。

    处理分工：
      · 前台活跃账号：
          1) live → 备份（同步 App 正在使用的最新 Cookie/Token，不关闭 App）
          2) 官方 OAuth 认证探针 + 额度查询，并安全接收可能轮换的凭证
             （前台 App 自身维持 live Session；此处同步备份并验证 OAuth）
      · 后台账号：
          HTTPS 额度校验；仅在令牌临期/被拒绝时运行官方 OAuth 探针
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
        print(f"  · 额度查询 / OAuth 校验 ..... 进行中")
        # 前台 App 是 live refresh token 的唯一所有者；守护进程只做 HTTPS 校验，
        # 禁止在临时副本中刷新同一凭据，避免 refresh-token rotation 竞争。
        active_info = silent_query_quota(
            current_active,
            probe_oauth=False,
            include_subscription=False,
        )
        status = active_info.get("status", "Unknown")
        ok = _is_query_ok(active_info)
        detail = status
        if ok:
            detail = f"OK ({format_rate_limit(active_info.get('rate_limit'), use_color=False)})"
        elif status == "OK" and active_info.get("auth_status") != "OK":
            detail = f"OAuth Error: {active_info.get('auth_status', 'Unknown')}"
        print(f"  · 额度查询 / OAuth 校验 ..... {_log_status(ok, detail)}")
        if active_info.get("auth_status") != "OK":
            print(f"  · OAuth 保活告警 ............ {active_info.get('auth_status', 'Unknown')}")
        _check_token_status(current_active, active_info, revoked_accounts)
        _record_usage_result(usage_cache, current_active, active_info)
        results.append(("前台", current_active, ok, detail))

    # ── 后台账号 ──────────────────────────────────────────────────
    for i, p in enumerate(background_profiles, 1):
        print(f"[后台 {i}/{len(background_profiles)}] {p}")
        print(f"  · HTTPS 校验 / 按需续签 .... 进行中")
        info = silent_query_quota(p, probe_oauth=True, include_subscription=False)
        status = info.get("status", "Unknown")
        ok = _is_query_ok(info)
        detail = status
        if ok:
            detail = f"OK ({format_rate_limit(info.get('rate_limit'), use_color=False)})"
        elif status == "OK" and info.get("auth_status") != "OK":
            detail = f"OAuth Error: {info.get('auth_status', 'Unknown')}"
        print(f"  · HTTPS 校验 / 按需续签 .... {_log_status(ok, detail)}")
        if info.get("auth_status") != "OK":
            print(f"  · OAuth 保活告警 ............ {info.get('auth_status', 'Unknown')}")
        _check_token_status(p, info, revoked_accounts)
        _record_usage_result(usage_cache, p, info)
        results.append(("后台", p, ok, detail))

    save_usage_cache(usage_cache)

    # ── 汇总 ──────────────────────────────────────────────────────
    ok_n = sum(1 for r in results if r[2])
    fail_n = len(results) - ok_n
    print(thin)
    print(f"本轮结果: 成功 {ok_n}  |  失败 {fail_n}  |  凭据失效确认 {len(revoked_accounts)}")
    if results:
        for role, name, ok, detail in results:
            mark = "OK" if ok else "FAIL"
            pretty_detail = _pretty_status_desc(detail)
            print(f"  [{mark}] ({role}) {name}: {pretty_detail}")
    if revoked_accounts:
        print("警告: 以下账号凭据已被连续确认拒绝，需重新登录后执行 add 重建备份:")
        for name in revoked_accounts:
            print(f"  ! {name}  ->  python3 codex_mgr.py switch {name}")
    print(f"前台客户端: 未关闭、未切换 (前台账号 '{current_active}' 使用中)")
    print(sep)


def cmd_wakeup():
    with operation_lock(timeout=2) as acquired:
        if not acquired:
            print("保活跳过: 另一个账号切换/刷新/保活操作正在运行")
            return False
        _cmd_wakeup_impl()
        return True


def _check_token_status(profile_name, info, revoked_accounts):
    """仅把连续、结构化确认的 401 记为凭据失效。"""
    if (
        (info or {}).get("status") in ("Token Expired", "Token Rejected")
        and (info or {}).get("auth_failure_confirmed")
    ):
        if profile_name not in revoked_accounts:
            revoked_accounts.append(profile_name)


def _acquire_daemon_singleton():
    """Python 进程自身持有 flock；shell PID 检测失误也不会产生双守护。"""
    global _DAEMON_LOCK_FD
    os.makedirs(CODEX_HOME, exist_ok=True)
    lock_file = open(DAEMON_LOCK_FILE, "a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return False
    _DAEMON_LOCK_FD = lock_file
    _atomic_write_text(DAEMON_PID_FILE, f"{os.getpid()}\n", mode=0o600)
    return True


def _release_daemon_singleton():
    global _DAEMON_LOCK_FD
    try:
        if os.path.exists(DAEMON_PID_FILE):
            with open(DAEMON_PID_FILE, "r") as pid_file:
                recorded_pid = pid_file.read().strip()
            if recorded_pid == str(os.getpid()):
                os.unlink(DAEMON_PID_FILE)
    except Exception:
        pass
    if _DAEMON_LOCK_FD is not None:
        try:
            fcntl.flock(_DAEMON_LOCK_FD.fileno(), fcntl.LOCK_UN)
            _DAEMON_LOCK_FD.close()
        except Exception:
            pass
        _DAEMON_LOCK_FD = None


def _start_caffeinate_for_daemon():
    """由已脱离终端的 Python 守护进程持有睡眠抑制子进程。"""
    try:
        proc = subprocess.Popen(
            ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _atomic_write_text(CAFFEINATE_PID_FILE, f"{proc.pid}\n", mode=0o600)
        return proc
    except Exception as exc:
        print(f"警告: caffeinate 启动失败，系统睡眠时保活可能暂停: {exc}")
        return None


def _stop_caffeinate_for_daemon(proc):
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    try:
        if os.path.exists(CAFFEINATE_PID_FILE):
            os.unlink(CAFFEINATE_PID_FILE)
    except Exception:
        pass


def cmd_daemon():
    if not _acquire_daemon_singleton():
        print("守护进程启动跳过: 已有实例持有单例锁")
        return

    # 标准 Unix Daemon 化：创建新会话，彻底脱离控制终端
    try:
        os.setsid()
    except OSError:
        pass  # 已是会话 leader，忽略

    caffeinate_proc = _start_caffeinate_for_daemon()

    def _sigterm_handler(signum, frame):
        print("守护进程收到停止信号，正在退出...")
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _sigterm_handler)

    try:
        configured_minutes = int(os.environ.get(
            DAEMON_INTERVAL_ENV,
            str(DEFAULT_DAEMON_INTERVAL_MINUTES),
        ))
    except ValueError:
        configured_minutes = DEFAULT_DAEMON_INTERVAL_MINUTES
    configured_minutes = min(1440, max(15, configured_minutes))
    interval_sec = configured_minutes * 60
    interval_min = interval_sec // 60
    cycle = 0

    print("=" * 60)
    print("Codex 保活守护进程已启动")
    print(f"  PID: {os.getpid()}")
    print(f"  周期: 每 {interval_min} 分钟执行一轮")
    print(f"  网络: {get_network_mode()} ({NETWORK_MODE_ENV})")
    print("  动作: 前台同步备份+HTTPS校验 | 后台HTTPS校验+按需OAuth续签")
    print("=" * 60)

    next_run = time.monotonic()
    try:
        while True:
            cycle += 1
            print("")
            print(f">>> 第 {cycle} 轮")
            try:
                ran = cmd_wakeup()
            except Exception as exc:
                ran = False
                print(f"本轮发生未捕获异常，守护进程将继续运行: {type(exc).__name__}: {exc}")

            # 低频 + 小幅随机抖动，避免所有账号长期以机器化固定节拍访问认证端点。
            if ran is False:
                next_run = time.monotonic() + 60
                delay = 60
            else:
                jittered_interval = interval_sec * random.uniform(0.9, 1.1)
                next_run = time.monotonic() + jittered_interval
                delay = max(1, next_run - time.monotonic())
            next_at = datetime.datetime.now() + datetime.timedelta(seconds=delay)
            next_str = next_at.strftime("%Y-%m-%d %H:%M:%S")
            print(f"本轮结束，预计下次: {next_str}")
            time.sleep(delay)
    except (KeyboardInterrupt, SystemExit):
        print("守护进程已安全退出。")
    finally:
        _stop_caffeinate_for_daemon(caffeinate_proc)
        _release_daemon_singleton()


def print_help():
    print(f"""
ChatGPT/Codex 多账号管理器 CLI

使用方法:
  python3 codex_mgr.py <command> [args]

可用命令:
  list              列出所有账号 Profile 及其订阅到期日、用量限额 (读取本地缓存)
  list --refresh    静默现查所有账号的限额、额度与订阅到期 (不打扰当前客户端，不弹窗)
  add <name>        将您当前的登录态备份另存为一个全新的 Profile 账号
  switch <name>     一键备份当前账号，无缝切换到目标账号并重新打开客户端
  switch new        准备一个干净的“未登录”客户端环境以供登录并录入新账号
  rename <old> <new> 安全重命名本地 Profile、活跃标记和用量缓存
  del/remove <name> 永久删除指定账号 Profile 的本地备份和用量缓存
  wakeup            手动执行一次批量刷新保活与用量更新
  daemon            在当前终端启动常驻后台守护进程 (建议使用 wakeup_daemon.sh 启动)
""")

def _disable_ansi_colors():
    """写入日志文件时关闭 ANSI 颜色，避免出现 [92m 这类乱码。"""
    global GREEN, RED, YELLOW, CYAN, BOLD, RESET
    GREEN = RED = YELLOW = CYAN = BOLD = RESET = ""


def _run_locked(action, timeout=60):
    with operation_lock(timeout=timeout) as acquired:
        if not acquired:
            print(f"{YELLOW}已有账号切换/刷新/保活操作正在运行，请稍后重试。{RESET}")
            return None
        return action()


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
        _run_locked(lambda: cmd_add(profile))
    elif cmd == "switch":
        if len(sys.argv) < 3:
            print("错误: 请指定要切换的目标账号 Profile 名称。")
            print("示例: python3 codex_mgr.py switch account1")
            sys.exit(1)
        _run_locked(lambda: cmd_switch(sys.argv[2]))
    elif cmd == "rename":
        if len(sys.argv) != 4:
            print("错误: 请指定旧名和新名。")
            print("示例: python3 codex_mgr.py rename old_profile new_profile")
            sys.exit(1)
        _run_locked(lambda: cmd_rename(sys.argv[2], sys.argv[3]))
    elif cmd in ["del", "remove"]:
        if len(sys.argv) < 3:
            print("错误: 请指定要删除的账号 Profile 名称。")
            print("示例: python3 codex_mgr.py del account1")
            sys.exit(1)
        _run_locked(lambda: cmd_del(sys.argv[2]))
    elif cmd == "wakeup":
        cmd_wakeup()
    elif cmd == "daemon":
        cmd_daemon()
    else:
        print_help()

if __name__ == "__main__":
    main()
