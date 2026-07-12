import os
import re

def scan_leveldb():
    db_path = os.path.expanduser("~/Library/Application Support/Codex/Default/Local Storage/leveldb")
    if not os.path.exists(db_path):
        print(f"LevelDB path not found: {db_path}")
        return
        
    print(f"Scanning LevelDB files in {db_path}...")
    
    jwt_ascii_re = re.compile(b'eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+')

    for root, dirs, files in os.walk(db_path):
        for file in files:
            if not file.endswith(('.log', '.ldb')):
                continue
                
            filepath = os.path.join(root, file)
            try:
                with open(filepath, 'rb') as f:
                    content = f.read()
                    
                # Search ASCII JWT
                ascii_matches = jwt_ascii_re.findall(content)
                if ascii_matches:
                    print(f"\n[ASCII JWT] Found in {file}:")
                    for m in ascii_matches[:3]:
                        print(f"  {m[:30].decode('ascii')}...")
                        
                # Search UTF-16LE JWT
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
                            jwt_val = m.group(0)
                            print(f"\n[UTF-16 JWT] Found in {file} at offset {idx}:")
                            print(f"  {jwt_val[:50]}... (len {len(jwt_val)})")
                    except Exception as e:
                        pass
                    idx += 8
                    
                # Search keywords
                for keyword in [b's\x00e\x00s\x00s\x00i\x00o\x00n\x00', b'a\x00c\x00c\x00e\x00s\x00s\x00_\x00t\x00o\x00k\x00e\x00n\x00']:
                    idx = 0
                    while True:
                        idx = content.find(keyword, idx)
                        if idx == -1:
                            break
                        start = max(0, idx - 20)
                        end = min(len(content), idx + 100)
                        context = content[start:end]
                        context_str = context.replace(b'\x00', b'.')
                        print(f"Keyword context in {file} at {idx}: {context_str}")
                        idx += len(keyword)
                        
            except Exception as e:
                print(f"Error reading {file}: {e}")

if __name__ == "__main__":
    scan_leveldb()
