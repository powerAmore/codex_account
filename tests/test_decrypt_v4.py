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

def is_mostly_printable(data):
    if not data:
        return False
    printable = sum(1 for b in data if 32 <= b <= 126 or b in (9, 10, 13))
    return (printable / len(data)) > 0.7

def decrypt_try(encrypted_value, key, iv):
    if len(encrypted_value) < 3 or encrypted_value[:3] != b'v10':
        return None
    encrypted_data = encrypted_value[3:]
    try:
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(encrypted_data) + decryptor.finalize()
        # strip padding if it seems valid, otherwise keep
        padding_len = decrypted[-1]
        if 1 <= padding_len <= 16:
            # check if padding is valid
            if all(b == padding_len for b in decrypted[-padding_len:]):
                decrypted = decrypted[:-padding_len]
        return decrypted
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
        print("No matching cookies in DB")
        return

    iv_space = b' ' * 16
    iv_zero = b'\x00' * 16

    # Test combinations
    for name_p, p in passwords.items():
        for algo in ['sha1', 'sha256']:
            for key_len in [16, 32]:
                for iter_count in [1003, 1000, 1]:
                    key = hashlib.pbkdf2_hmac(algo, p, b'saltysalt', iter_count, key_len)
                    for name_iv, iv in [("space", iv_space), ("zero", iv_zero)]:
                        # test on the first cookie
                        host_key, cname, val = rows[0]
                        dec = decrypt_try(val, key, iv)
                        if dec and is_mostly_printable(dec):
                            print(f"\nSUCCESS Combination Found!")
                            print(f"Password: {name_p}")
                            print(f"Hash: {algo}")
                            print(f"Key length: {key_len}")
                            print(f"Iterations: {iter_count}")
                            print(f"IV: {name_iv}")
                            print(f"Decrypted first cookie ({cname}): {dec.decode('utf-8', errors='ignore')}")
                            
                            # Decrypt all
                            print("\nAll decrypted cookies:")
                            for hk, cn, v in rows:
                                d = decrypt_try(v, key, iv)
                                val_str = d.decode('utf-8', errors='ignore') if d else "Failed"
                                print(f"  {hk} | {cn} = {val_str}")
                            return

    print("No combination succeeded in decrypting to printable ASCII.")

if __name__ == "__main__":
    main()
