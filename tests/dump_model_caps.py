import json

def main():
    try:
        with open("/tmp/codex_models.json", "r") as f:
            data = json.load(f)
            
        models = data.get("models", [])
        for i, m in enumerate(models):
            print(f"\n=====================================")
            print(f"Model ID: {m.get('id')}")
            print(f"Title: {m.get('title')}")
            
            # Print keys and values of this model to see what cap info is there
            for k, v in m.items():
                if k not in ['description', 'system_instructions', 'instructions_variables']:
                    print(f"  {k}: {v}")
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    main()
