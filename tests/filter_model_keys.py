import json

def main():
    try:
        with open("/tmp/codex_models.json", "r") as f:
            data = json.load(f)
            
        models = data.get("models", [])
        for m in models:
            print(f"\nModel ID: {m.get('id')}")
            # recursively search keys for target keywords
            def search_dict(d, path=""):
                if isinstance(d, dict):
                    for k, v in d.items():
                        new_path = f"{path}.{k}" if path else k
                        if any(kw in k.lower() for kw in ["cap", "limit", "max", "usage", "quota"]):
                            print(f"  {new_path}: {v}")
                        search_dict(v, new_path)
                elif isinstance(d, list):
                    for idx, item in enumerate(d):
                        search_dict(item, f"{path}[{idx}]")
            search_dict(m)
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    main()
