import sqlite3
import os
import json

def main():
    db_path = os.path.expanduser("~/.codex/state_5.sqlite")
    if not os.path.exists(db_path):
        print("Database not found")
        return
        
    print(f"Connecting to database: {db_path}")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # 1. 查找表名
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [row[0] for row in cursor.fetchall()]
    print("Tables:", tables)
    
    # 2. 遍历表并打印它们的前几行数据以查明内容
    for table in tables:
        try:
            # 获取列名
            cursor.execute(f"PRAGMA table_info({table});")
            cols = [col[1] for col in cursor.fetchall()]
            print(f"\n--- Table: {table} (Columns: {cols}) ---")
            
            cursor.execute(f"SELECT * FROM {table} LIMIT 10;")
            rows = cursor.fetchall()
            for r in rows:
                print(r)
        except Exception as e:
            print(f"Error reading table {table}: {e}")
            
    conn.close()

if __name__ == "__main__":
    main()
