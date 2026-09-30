#!/usr/bin/env python3
"""Export readable messages that are still only present in a Codex rollout.

The source rollout and SQLite database are opened read-only.  The projection
cursor is used as the lower bound, so this does not make assumptions about
JSONL physical line numbers (rollout ordinals and line numbers can differ).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


THREAD_ID = "01a05ac7-f077-7611-9bef-aa88d2f30190"
DEFAULT_ROLLOUT = Path(
    "/Users/powerlee/.codex/sessions/2026/09/01/"
    "rollout-2026-09-01T10-24-07-01a05ac7-f077-7611-9bef-aa88d2f30190.jsonl"
)
DEFAULT_DB = Path("/Users/powerlee/.codex/thread_history_1.sqlite")
DEFAULT_OUTPUT = Path("browser-compatibility-unprojected.md")


def projection_cursor(
    db_path: Path, thread_id: str
) -> tuple[int, int | None, int | None, int | None]:
    """Return (next ordinal, byte offset, max projected ordinal, item count).

    ``immutable=1`` prevents SQLite from trying to create journal/WAL files,
    which is important when inspecting the live Codex database safely.
    """

    uri = f"file:{db_path}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as con:
        row = con.execute(
            "SELECT next_rollout_ordinal, next_rollout_byte_offset "
            "FROM thread_history_projection_state WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"No projection state found for thread {thread_id}")
        max_row = con.execute(
            "SELECT MAX(rollout_ordinal), COUNT(*) FROM thread_items WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
    return (
        int(row[0]),
        row[1],
        max_row[0] if max_row else None,
        max_row[1] if max_row else None,
    )


def message_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    for content in payload.get("content") or []:
        if not isinstance(content, dict):
            continue
        if content.get("type") not in {"input_text", "output_text"}:
            continue
        text = content.get("text")
        if isinstance(text, str) and text:
            chunks.append(text)
    return "\n\n".join(chunks).strip()


def iter_unprojected_messages(
    rollout_path: Path, min_ordinal: int
) -> Iterable[dict[str, Any]]:
    with rollout_path.open("r", encoding="utf-8") as stream:
        for physical_line, line in enumerate(stream, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            ordinal = event.get("ordinal")
            payload = event.get("payload")
            if not isinstance(ordinal, int) or ordinal < min_ordinal:
                continue
            if event.get("type") != "response_item" or not isinstance(payload, dict):
                continue
            if payload.get("type") != "message" or payload.get("role") not in {
                "user",
                "assistant",
            }:
                continue
            text = message_text(payload)
            if not text:
                continue
            yield {
                "physical_line": physical_line,
                "ordinal": ordinal,
                "timestamp": event.get("timestamp"),
                "role": payload.get("role"),
                "phase": payload.get("phase"),
                "turn_id": (payload.get("internal_chat_message_metadata_passthrough") or {}).get(
                    "turn_id"
                ),
                "message_id": payload.get("id"),
                "text": text,
            }


def iter_task_errors(rollout_path: Path, min_ordinal: int) -> Iterable[dict[str, Any]]:
    """Collect task-complete errors in the unprojected range for context."""

    with rollout_path.open("r", encoding="utf-8") as stream:
        for physical_line, line in enumerate(stream, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event.get("ordinal"), int) or event["ordinal"] < min_ordinal:
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "task_complete":
                continue
            error = payload.get("error")
            if isinstance(error, dict) and error.get("message"):
                yield {
                    "physical_line": physical_line,
                    "ordinal": event["ordinal"],
                    "turn_id": payload.get("turn_id"),
                    "message": error.get("message"),
                }


def render(
    *,
    output_path: Path,
    thread_id: str,
    rollout_path: Path,
    db_path: Path,
    next_ordinal: int,
    projection_max: int | None,
    item_count: int | None,
    messages: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# 未投影的 Codex 对话内容",
        "",
        "> 这是从原始 Rollout JSONL 只读提取的恢复副本；不会修改 Rollout 或 SQLite。",
        "",
        "## 元数据",
        "",
        f"- 线程 ID：`{thread_id}`",
        f"- Rollout：`{rollout_path}`",
        f"- SQLite：`{db_path}`",
        f"- 导出时间（UTC）：`{generated}`",
        f"- SQLite 下一待投影事件序号：`{next_ordinal}`",
        f"- SQLite 已投影消息最大事件序号：`{projection_max}`",
        f"- SQLite 已投影消息数：`{item_count}`",
        f"- 本文件包含事件序号 `>= {next_ordinal}` 的可读 user/assistant 消息。",
        "",
        "## 消息",
        "",
    ]
    if not messages:
        lines.append("未找到可读的未投影 user/assistant 消息。")
    else:
        for index, item in enumerate(messages, start=1):
            role = "用户" if item["role"] == "user" else "助手"
            phase = f"，phase: `{item['phase']}`" if item.get("phase") else ""
            lines.extend(
                [
                    f"### {index}. {role}{phase}",
                    "",
                    f"- 事件序号：`{item['ordinal']}`",
                    f"- Rollout 物理行：`{item['physical_line']}`",
                    f"- 时间：`{item.get('timestamp')}`",
                    f"- turn ID：`{item.get('turn_id') or '未知'}`",
                    f"- message ID：`{item.get('message_id') or '未知'}`",
                    "",
                    item["text"],
                    "",
                ]
            )
    if errors:
        lines.extend(["## 未投影轮次错误", ""])
        for error in errors:
            lines.extend(
                [
                    f"- 事件序号 `{error['ordinal']}`（物理行 `{error['physical_line']}`，turn `{error.get('turn_id')}`）：{error['message']}",
                ]
            )
        lines.append("")
    output_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rollout", type=Path, default=DEFAULT_ROLLOUT)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--thread-id", default=THREAD_ID)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    next_ordinal, _byte_offset, projection_max, item_count = projection_cursor(
        args.db, args.thread_id
    )
    messages = list(iter_unprojected_messages(args.rollout, next_ordinal))
    errors = list(iter_task_errors(args.rollout, next_ordinal))
    render(
        output_path=args.output,
        thread_id=args.thread_id,
        rollout_path=args.rollout,
        db_path=args.db,
        next_ordinal=next_ordinal,
        projection_max=projection_max,
        item_count=item_count,
        messages=messages,
        errors=errors,
    )
    print(f"Exported {len(messages)} messages and {len(errors)} task errors to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
