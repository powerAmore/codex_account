import os
import subprocess
import sqlite3
import base64
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

def get_keychain_password():
    cmd = ["security", "find-generic-password", "-w", "-s", "Codex Safe Storage", "-a", "Codex Key"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise Exception(f"Failed to get password from Keychain: {res.stderr.strip()}")
    return res.stdout.strip()

def decrypt_cbc(encrypted_value, key):
    if len(encrypted_value) < 3 or encrypted_value[:3] != b'v10':
        return None
    encrypted_data = encrypted_value[3:]
    iv_space = b' ' * 16
    iv_zero = b'\x00' * 16
    for iv in [iv_space, iv_zero]:
        try:
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
            decryptor = cipher.decryptor()
            decrypted = decryptor.update(encrypted_data) + decryptor.finalize()
            padding_len = decrypted[-1]
            if 1 <= padding_len <= 16:
                if all(b == padding_len for b in decrypted[-padding_len:]):
                    decrypted = decrypted[:-padding_len]
            # check if printable
            val = decrypted.decode('utf-8')
            if val.isprintable() and len(val) > 0:
                return f"CBC(IV:{'space' if iv == iv_space else 'zero'}) -> {val}"
        except Exception:
            pass
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
        val = decrypted.decode('utf-8')
        if val.isprintable() and len(val) > 0:
            return f"GCM -> {val}"
    except Exception:
        pass
    return None

def main():
    try:
        password_str = get_keychain_password()
        print(f"Password string: {password_str}")
    except Exception as e:
        print(f"Error: {e}")
        return

    keys = {
        "str": password_str.encode('utf-8'),
        "str_pad16": password_str.encode('utf-8')[:16],  # first 16 bytes of string
        "str_pad32": (password_str.encode('utf-8') * 2)[:32], # 32 bytes
    }
    try:
        keys["bytes"] = base64.b64decode(password_str)
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

    for name_k, key in keys.items():
        if len(key) not in [16, 24, 32]:
            continue
        print(f"\nTesting direct key: {name_k} (length {len(key)})")
        
        # Test first cookie
        host_key, cname, val = rows[0]
        
        # Try CBC
        res = decrypt_cbc(val, key)
        if res:
            print(f"  SUCCESS! {cname}: {res}")
            return
            
        # Try GCM
        res = decrypt_gcm(val, key)
        if res:
            print(f"  SUCCESS! {cname}: {res}")
            return

    print("\nAll direct key GCM/CBC tests failed.")

if __name__ == "__main__":
    main()
