import os
import subprocess
import sqlite3
import hashlib
import base64
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

def get_keychain_password():
    cmd = ["security", "find-generic-password", "-w", "-s", "Codex Safe Storage", "-a", "Codex Key"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise Exception(f"Failed to get password from Keychain: {res.stderr.strip()}")
    return res.stdout.strip()

def decrypt_gcm(encrypted_value, key):
    if len(encrypted_value) < 3 or encrypted_value[:3] != b'v10':
        return None
        
    # GCM format: prefix 'v10' (3 bytes) + nonce (12 bytes) + ciphertext + tag (16 bytes)
    if len(encrypted_value) < 3 + 12 + 16:
        return None
        
    nonce = encrypted_value[3:15]
    ciphertext_and_tag = encrypted_value[15:]
    ciphertext = ciphertext_and_tag[:-16]
    tag = ciphertext_and_tag[-16:]
    
    try:
        cipher = Cipher(algorithms.AES(key), modes.GCM(nonce, tag), backend=default_backend())
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(ciphertext) + decryptor.finalize()
        return decrypted.decode('utf-8')
    except Exception:
        return None

def main():
    try:
        password_str = get_keychain_password()
        print(f"Password string: {password_str}")
    except Exception as e:
        print(f"Error: {e}")
        return

    passwords = {
        "str": password_str.encode('utf-8'),
    }
    try:
        passwords["bytes"] = base64.b64decode(password_str)
    except Exception:
        pass

    cookie_path = os.path.expanduser("~/Library/Application Support/Codex/Default/Cookies")
    if not os.path.exists(cookie_path):
        print("Cookie file not found")
        return
        
    conn = sqlite3.connect(cookie_path)
    cursor = conn.cursor()
    cursor.execute("SELECT host_key, name, encrypted_value FROM cookies WHERE host_key LIKE '%openai%' OR host_key LIKE '%chatgpt%' LIMIT 5")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("No matching cookies")
        return

    # Try both key lengths (16 for GCM-128, 32 for GCM-256)
    for name_p, p in passwords.items():
        for key_len in [16, 32]:
            key = hashlib.pbkdf2_hmac('sha1', p, b'saltysalt', 1003, key_len)
            # Try to decrypt the first cookie
            host_key, cname, val = rows[0]
            dec = decrypt_gcm(val, key)
            if dec:
                print(f"\nSUCCESS GCM Decryption Found!")
                print(f"Password source: {name_p}")
                print(f"Key length: {key_len}")
                print(f"Decrypted cookie: {cname} = {dec[:20]}...")
                
                print("\nAll cookies decrypted:")
                for hk, cn, v in rows:
                    d = decrypt_gcm(v, key)
                    print(f"  {hk} | {cn} = {d}")
                return

    print("GCM Decryption failed for all combinations.")

if __name__ == "__main__":
    main()
