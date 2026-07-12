import sqlite3
import os
import json

def main():
    db_path = os.path.expanduser("~/.codex/logs_2.sqlite")
    if not os.path.exists(db_path):
        print("Database not found")
        return
        
    print(f"Connecting to database: {db_path}")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # 1. 查找所有的表名
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [row[0] for row in cursor.fetchall()]
    print("Tables:", tables)
    
    # 2. 假设有些日志记录表，我们在里面搜索包含 usage, limits, backend-api, chatgpt 等关键字的行
    # 我们遍历表并查找包含特定关键字的内容
    for table in tables:
        try:
            # 获取列名
            cursor.execute(f"PRAGMA table_info({table});")
            cols = [col[1] for col in cursor.fetchall()]
            
            # 我们寻找文本列
            text_cols = []
            for col in cols:
                # 模糊查询看看列类型，或者直接对所有列查
                text_cols.append(col)
                
            if not text_cols:
                continue
                
            # 对该表进行查询
            for col in text_cols:
                query = f"SELECT {col} FROM {table} WHERE {col} LIKE '%usage%' OR {col} LIKE '%limits%' OR {col} LIKE '%backend-api%' LIMIT 5;"
                cursor.execute(query)
                rows = cursor.fetchall()
                if rows:
                    print(f"\nMatches in table '{table}', column '{col}':")
                    for r in rows:
                        val = str(r[0])
                        print(val[:200])
        except Exception as e:
            pass
            
    conn.close()

if __name__ == "__main__":
    main()
