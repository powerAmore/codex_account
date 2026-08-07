#!/usr/bin/env python3
"""
扫描 macOS Codex 应用存储目录（LevelDB 本地存储与 state_5.sqlite 数据库）的 Token 与数据工具。
"""
import os
import re
import sqlite3

def scan_leveldb(db_path=None):
    if not db_path:
        db_path = os.path.expanduser("~/Library/Application Support/Codex/Default/Local Storage/leveldb")
    if not os.path.exists(db_path):
        print(f"[!] LevelDB path not found: {db_path}")
        return

    print(f"\n=== Scanning LevelDB in {db_path} ===")
    jwt_ascii_re = re.compile(b'eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+')

    for root, dirs, files in os.walk(db_path):
        for file in files:
            if not file.endswith(('.log', '.ldb')):
                continue

            filepath = os.path.join(root, file)
            try:
                with open(filepath, 'rb') as f:
                    content = f.read()

                ascii_matches = jwt_ascii_re.findall(content)
                if ascii_matches:
                    print(f"\n[ASCII JWT] Found in {file}:")
                    for m in ascii_matches[:3]:
                        print(f"  {m[:40].decode('ascii')}...")

                idx = 0
                while True:
                    idx = content.find(b'e\x00y\x00J\x00', idx)
                    if idx == -1:
                        break
                    chunk = content[idx:idx+3000]
                    try:
                        decoded = chunk.decode('utf-16le', errors='ignore')
                        m = re.search(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', decoded)
                        if m:
                            print(f"\n[UTF-16 JWT] Found in {file} at offset {idx}:")
                            print(f"  {m.group(0)[:50]}...")
                    except Exception:
                        pass
                    idx += 8

            except Exception as e:
                print(f"Error reading {file}: {e}")

def scan_sqlite(db_path=None):
    if not db_path:
        db_path = os.path.expanduser("~/.codex/state_5.sqlite")
    if not os.path.exists(db_path):
        print(f"[!] SQLite DB not found: {db_path}")
        return

    print(f"\n=== Scanning SQLite DB: {db_path} ===")
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cursor.fetchall()]
        print("Tables:", tables)

        for table in tables:
            cursor.execute(f"PRAGMA table_info({table});")
            cols = [col[1] for col in cursor.fetchall()]
            print(f"\n--- Table: {table} (Columns: {cols}) ---")

            cursor.execute(f"SELECT * FROM {table} LIMIT 5;")
            for r in cursor.fetchall():
                print(" ", r)
        conn.close()
    except Exception as e:
        print(f"Error reading SQLite database: {e}")

if __name__ == "__main__":
    scan_leveldb()
    scan_sqlite()
