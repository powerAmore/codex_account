#!/usr/bin/env python3
"""
ChatGPT / Codex 模型 JSON 结构分析与过滤工具。
读取无头浏览器或 API 抓取的 /tmp/codex_models.json 缓存并分析字段。
"""
import os
import sys
import json

def inspect_models(file_path="/tmp/codex_models.json"):
    if not os.path.exists(file_path):
        print(f"Model cache file not found at: {file_path}")
        return

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        models = data.get("models", [])
        print(f"=== Found {len(models)} Models in {file_path} ===")

        for idx, m in enumerate(models, 1):
            model_id = m.get('id', 'Unknown')
            title = m.get('title', 'No Title')
            print(f"\n[{idx}] Model ID: {model_id} | Title: {title}")

            # 打印非巨大文本字段的属性
            filtered_info = {k: v for k, v in m.items() if k not in ['description', 'system_instructions', 'instructions_variables']}
            print("  Main Attributes:")
            for k, v in filtered_info.items():
                print(f"    - {k}: {v}")

            # 检索包含 limit/quota/cap 的敏感属性
            matched_keys = []
            def search_keys(d, path=""):
                if isinstance(d, dict):
                    for k, v in d.items():
                        new_path = f"{path}.{k}" if path else k
                        if any(kw in k.lower() for kw in ["cap", "limit", "max", "usage", "quota"]):
                            matched_keys.append((new_path, v))
                        search_keys(v, new_path)
                elif isinstance(d, list):
                    for i, item in enumerate(d):
                        search_keys(item, f"{path}[{i}]")

            search_keys(m)
            if matched_keys:
                print("  Matched Cap/Limit Fields:")
                for pk, pv in matched_keys:
                    print(f"    * {pk}: {pv}")

    except Exception as e:
        print(f"Error inspecting models: {e}")

if __name__ == "__main__":
    file_target = sys.argv[1] if len(sys.argv) > 1 else "/tmp/codex_models.json"
    inspect_models(file_target)
