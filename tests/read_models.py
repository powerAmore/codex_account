import json

def main():
    try:
        with open("/tmp/codex_models.json", "r") as f:
            data = json.load(f)
        
        models = data.get("models", [])
        for i, m in enumerate(models):
            print(f"\nModel {i+1}: {m.get('id')} - {m.get('title')}")
            # print everything except huge descriptions if any
            m_copy = {k: v for k, v in m.items() if k not in ['description', 'system_instructions']}
            print(json.dumps(m_copy, indent=2))
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    main()
