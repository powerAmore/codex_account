import sqlite3
import os
import re

def main():
    db_path = os.path.expanduser("~/.codex/logs_2.sqlite")
    if not os.path.exists(db_path):
        print("Database not found")
        return
        
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # 查找所有的包含 backend-api 或者 chatgpt.com 字段的行
    cursor.execute("SELECT feedback_log_body FROM logs WHERE feedback_log_body LIKE '%backend-api%' OR feedback_log_body LIKE '%chatgpt.com%';")
    rows = cursor.fetchall()
    print(f"Total matching rows: {len(rows)}")
    
    url_pattern = re.compile(r'https?://[^\s"\'}]+')
    
    found_urls = set()
    for row in rows:
        row_str = str(row)
        urls = url_pattern.findall(row_str)
        for u in urls:
            found_urls.add(u)
            
    print("\n--- Found URLs in all log rows ---")
    for u in sorted(found_urls):
        print(u)
        
    conn.close()

if __name__ == "__main__":
    main()
