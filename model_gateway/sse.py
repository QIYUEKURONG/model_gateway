"""SSE 帧的编码与解析。网关向外只吐统一事件，不把供应商原文直接交出去。"""

from __future__ import annotations

import json
from typing import Any, Iterable, Iterator


def encode_sse(event: str, data: dict[str, Any]) -> str:
    """编一条帧：event 行、data 行、空行。data 是一行紧凑 JSON。"""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=_json_default)
    return f"event: {event}\ndata: {payload}\n\n"


def decode_sse(frame: str) -> tuple[str, dict[str, Any]]:
    event = "message"
    data = ""
    for line in frame.splitlines():
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            raw = line.split(":", 1)[1]
            data = raw[1:] if raw.startswith(" ") else raw
    return event, json.loads(data) if data else {}


def parse_sse(lines: Iterable[str]) -> Iterator[tuple[str, str]]:
    """从供应商的原始行流里切出事件。以冒号开头的行是注释，丢掉。"""
    event = "message"
    data_lines: list[str] = []
    for raw_line in lines:
        line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        if line == "":
            if data_lines:
                yield event, "\n".join(data_lines)
            event = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
            continue
        if line.startswith("data:"):
            value = line.split(":", 1)[1]
            if value.startswith(" "):
                value = value[1:]
            data_lines.append(value)
    if data_lines:
        yield event, "\n".join(data_lines)


def _json_default(value: Any) -> Any:
    if hasattr(value, "value") and not isinstance(value, (str, int, float)):
        return value.value
    raise TypeError(f"无法编码为 JSON: {type(value).__name__}")
