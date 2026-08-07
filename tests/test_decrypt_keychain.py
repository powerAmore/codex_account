#!/usr/bin/env python3
"""
Keychain 密码读取与 Chrome/Electron 数据库 Cookie/Session 解密测试脚本。
整合了 Keychain 凭证提取、PBKDF2 密钥衍生、AES-CBC 及 AES-GCM 解密算法验证。
"""
import os
import sys
import subprocess
import sqlite3
import hashlib
import base64
import ctypes
import ctypes.util
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

def get_keychain_password_cli():
    """使用 security 命令行获取 Keychain 密码字符串"""
    cmd = ["security", "find-generic-password", "-w", "-s", "Codex Safe Storage", "-a", "Codex Key"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise Exception(f"Failed to get password from Keychain CLI: {res.stderr.strip()}")
    return res.stdout.strip()

def get_keychain_password_ctypes():
    """使用 Ctypes 方式直接调用 Security framework 获取 Keychain 密码 Raw Bytes"""
    sec_path = ctypes.util.find_library('Security')
    if not sec_path:
        raise Exception("Security framework not found")
    sec = ctypes.CDLL(sec_path)
    
    sec.SecKeychainFindGenericPassword.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
        ctypes.c_uint32, ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p
    ]
    sec.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    service_bytes = b"Codex Safe Storage"
    account_bytes = b"Codex Key"
    password_length = ctypes.c_uint32(0)
    password_data = ctypes.c_void_p(0)

    status = sec.SecKeychainFindGenericPassword(
        None, len(service_bytes), service_bytes,
        len(account_bytes), account_bytes,
        ctypes.byref(password_length), ctypes.byref(password_data), None
    )
    if status != 0:
        raise Exception(f"SecKeychainFindGenericPassword status {status}")

    try:
        length = password_length.value
        data_ptr = password_data.value
        if not data_ptr:
            return b""
        return ctypes.string_at(data_ptr, length)
    finally:
        sec.SecKeychainItemFreeContent(None, password_data)

def is_printable_text(data):
    if not data:
        return False
    printable = sum(1 for b in data if 32 <= b <= 126 or b in (9, 10, 13))
    return (printable / len(data)) > 0.7

def decrypt_cbc(encrypted_value, key, iv):
    if len(encrypted_value) < 3 or encrypted_value[:3] != b'v10':
        return None
    encrypted_data = encrypted_value[3:]
    try:
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(encrypted_data) + decryptor.finalize()
        padding_len = decrypted[-1]
        if 1 <= padding_len <= 16:
            if all(b == padding_len for b in decrypted[-padding_len:]):
                decrypted = decrypted[:-padding_len]
        return decrypted
    except Exception:
        return None

def decrypt_gcm(encrypted_value, key):
    if len(encrypted_value) < 3 + 12 + 16 or encrypted_value[:3] != b'v10':
        return None
    nonce = encrypted_value[3:15]
    ciphertext_and_tag = encrypted_value[15:]
    ciphertext = ciphertext_and_tag[:-16]
    tag = ciphertext_and_tag[-16:]
    try:
        cipher = Cipher(algorithms.AES(key), modes.GCM(nonce, tag), backend=default_backend())
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(ciphertext) + decryptor.finalize()
        return decrypted
    except Exception:
        return None

def main():
    print("=== Keychain 密码获取测试 ===")
    password_str = None
    try:
        password_str = get_keychain_password_cli()
        print(f"[-] Keychain Password (CLI): {password_str}")
    except Exception as e:
        print(f"[!] Keychain CLI Error: {e}")

    try:
        raw_pw = get_keychain_password_ctypes()
        print(f"[-] Keychain Raw Bytes (Ctypes): {raw_pw.hex()}")
    except Exception as e:
        print(f"[!] Keychain Ctypes Error: {e}")

    if not password_str:
        print("无法从 Keychain 获取密码，退出解密测试。")
        return

    cookie_path = os.path.expanduser("~/Library/Application Support/Codex/Default/Cookies")
    if not os.path.exists(cookie_path):
        print(f"未找到 Cookie 数据库: {cookie_path}")
        return

    conn = sqlite3.connect(cookie_path)
    cursor = conn.cursor()
    cursor.execute("SELECT host_key, name, encrypted_value FROM cookies WHERE host_key LIKE '%openai%' OR host_key LIKE '%chatgpt%' LIMIT 5")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("未找到相关的 Cookie 条目。")
        return

    print(f"\n=== 尝试针对 {len(rows)} 条 Cookie 进行解密组合测试 ===")
    
    passwords = {"str": password_str.encode('utf-8')}
    try:
        passwords["bytes"] = base64.b64decode(password_str)
    except Exception:
        pass

    iv_space = b' ' * 16
    iv_zero = b'\x00' * 16

    found = False
    for name_p, p in passwords.items():
        for algo in ['sha1', 'sha256']:
            for key_len in [16, 32]:
                for iter_count in [1003, 1000, 1]:
                    key = hashlib.pbkdf2_hmac(algo, p, b'saltysalt', iter_count, key_len)
                    for name_iv, iv in [("space", iv_space), ("zero", iv_zero)]:
                        for host_key, cname, val in rows:
                            # 尝试 CBC
                            dec = decrypt_cbc(val, key, iv)
                            if dec and is_printable_text(dec):
                                print(f"\n[✓] 发现成功解密组合 (CBC)!")
                                print(f"    Password Mode: {name_p} | Hash: {algo} | KeyLen: {key_len} | Iter: {iter_count} | IV: {name_iv}")
                                print(f"    示例 ({cname}): {dec.decode('utf-8', errors='ignore')}")
                                found = True
                                break
                            # 尝试 GCM
                            dec_gcm = decrypt_gcm(val, key)
                            if dec_gcm and is_printable_text(dec_gcm):
                                print(f"\n[✓] 发现成功解密组合 (GCM)!")
                                print(f"    Password Mode: {name_p} | Hash: {algo} | KeyLen: {key_len} | Iter: {iter_count}")
                                print(f"    示例 ({cname}): {dec_gcm.decode('utf-8', errors='ignore')}")
                                found = True
                                break
                        if found:
                            break
                    if found:
                        break
                if found:
                    break
            if found:
                break
        if found:
            break

    if not found:
        print("[!] 组合测试完毕，未发现有效明文解密匹配（可能数据未加密或采用了其他 Key 规则）。")

if __name__ == "__main__":
    main()
