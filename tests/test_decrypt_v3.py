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

def decrypt(encrypted_value, key, iv):
    if len(encrypted_value) < 3 or encrypted_value[:3] != b'v10':
        return None
    encrypted_data = encrypted_value[3:]
    try:
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(encrypted_data) + decryptor.finalize()
        # PKCS7 padding
        padding_len = decrypted[-1]
        if 1 <= padding_len <= 16:
            decrypted = decrypted[:-padding_len]
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

    iv_space = b' ' * 16
    iv_zero = b'\x00' * 16

    for name_p, p in passwords.items():
        for key_len in [16, 32]:
            key = hashlib.pbkdf2_hmac('sha1', p, b'saltysalt', 1003, key_len)
            for name_iv, iv in [("space", iv_space), ("zero", iv_zero)]:
                # Try to decrypt the first cookie
                host_key, cname, val = rows[0]
                decrypted = decrypt(val, key, iv)
                if decrypted is not None and len(decrypted) > 0 and decrypted.isprintable():
                    print(f"\nSUCCESS Combination Found!")
                    print(f"Password source: {name_p}")
                    print(f"Key length: {key_len} bytes")
                    print(f"IV: {name_iv}")
                    print(f"Decrypted cookie: {cname} = {decrypted[:20]}...")
                    
                    # Decrypt all
                    print("\nAll cookies for this combination:")
                    for hk, cn, v in rows:
                        dec = decrypt(v, key, iv)
                        print(f"  Host: {hk} | {cn} = {dec}")
                    return

    print("All combinations failed to produce printable UTF-8 cookie value.")

if __name__ == "__main__":
    main()
