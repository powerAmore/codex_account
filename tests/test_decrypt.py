import os
import subprocess
import sqlite3
import hashlib
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

def get_keychain_password():
    cmd = ["security", "find-generic-password", "-w", "-s", "Codex Safe Storage", "-a", "Codex Key"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise Exception(f"Failed to get password from Keychain: {res.stderr.strip()}")
    return res.stdout.strip()

def decrypt_chrome_cookie(encrypted_value, key):
    if len(encrypted_value) < 3 or encrypted_value[:3] != b'v10':
        return f"[raw] {encrypted_value.decode('utf-8', errors='ignore')}"
    
    encrypted_data = encrypted_value[3:]
    iv = b' ' * 16
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    decrypted = decryptor.update(encrypted_data) + decryptor.finalize()
    
    # Strip padding (PKCS7)
    padding_len = decrypted[-1]
    if 1 <= padding_len <= 16:
         decrypted = decrypted[:-padding_len]
    try:
        return decrypted.decode('utf-8')
    except Exception:
        return f"[hex] {decrypted.hex()}"

def main():
    try:
        password = get_keychain_password()
        print(f"Password: {password}")
    except Exception as e:
        print(f"Error: {e}")
        return

    # Try both 16-byte (AES-128) and 32-byte (AES-256) keys
    key_128 = hashlib.pbkdf2_hmac('sha1', password.encode('utf-8'), b'saltysalt', 1003, 16)
    key_256 = hashlib.pbkdf2_hmac('sha1', password.encode('utf-8'), b'saltysalt', 1003, 32)
    
    cookie_path = os.path.expanduser("~/Library/Application Support/Codex/Default/Cookies")
    if not os.path.exists(cookie_path):
        print(f"Cookie file not found at {cookie_path}")
        return
        
    conn = sqlite3.connect(cookie_path)
    cursor = conn.cursor()
    
    # Get a few cookies to inspect
    cursor.execute("SELECT host_key, name, encrypted_value FROM cookies WHERE host_key LIKE '%openai%' OR host_key LIKE '%chatgpt%' LIMIT 5")
    rows = cursor.fetchall()
    
    for host_key, name, encrypted_value in rows:
        print(f"\nHost: {host_key} | Name: {name}")
        val_128 = decrypt_chrome_cookie(encrypted_value, key_128)
        val_256 = decrypt_chrome_cookie(encrypted_value, key_256)
        print(f"  AES-128: {val_128}")
        print(f"  AES-256: {val_256}")
            
    conn.close()

if __name__ == "__main__":
    main()
