"""结构化输出：统一 response_format，并在本地校验 JSON。"""

from __future__ import annotations

import json
import re
from typing import Any

from model_gateway.errors import ErrorCode, GatewayError, SchemaError
from model_gateway.models import ChatMessage, ResponseFormat, Role

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def deepseek_response_format(fmt: ResponseFormat | None) -> dict[str, str] | None:
    if fmt is None or fmt.type == "text":
        return None
    return {"type": "json_object"}


def json_schema_instruction(fmt: ResponseFormat | None) -> str | None:
    if fmt is None or fmt.type != "json_schema" or not fmt.json_schema:
        return None
    return (
        "Respond with valid json matching this JSON Schema. Do not add markdown fences.\n"
        + json.dumps(fmt.json_schema, ensure_ascii=False)
    )


def ensure_json_word(messages: list[ChatMessage], fmt: ResponseFormat | None) -> list[ChatMessage]:
    """DeepSeek 的 JSON 模式要求提示里出现 json 这个词。"""
    if fmt is None or fmt.type == "text":
        return list(messages)
    blob = "\n".join(message.content for message in messages).lower()
    if "json" in blob:
        return list(messages)
    hint = ChatMessage(role=Role.SYSTEM, content="Respond with valid json.")
    return [hint, *messages]


def anthropic_output_config(fmt: ResponseFormat | None) -> dict[str, Any] | None:
    if fmt is None or fmt.type == "text":
        return None
    if fmt.type == "json_schema":
        schema = fmt.json_schema
    else:
        schema = {"type": "object", "additionalProperties": True}
    return {"format": {"type": "json_schema", "schema": schema}}


def parse_model_output(text: str, fmt: ResponseFormat | None) -> Any:
    """text 格式返回 None。其余先去掉 ```json 围栏再解析；json_schema 再做本地校验。"""
    if fmt is None or fmt.type == "text":
        return None
    raw = _strip_fence(text)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        snippet = raw[:200]
        raise GatewayError(
            ErrorCode.PARSE_ERROR,
            f"模型没有返回合法 JSON: {exc.msg}；片段: {snippet}",
        ) from exc
    if fmt.type == "json_schema" and fmt.json_schema:
        _assert_schema(parsed, fmt.json_schema, "$")
    return parsed


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = _FENCE.sub("", stripped).strip()
    return stripped


def _assert_schema(instance: Any, schema: dict[str, Any], path: str) -> None:
    """只认 type、enum、required、properties、additionalProperties 和数组 items。"""
    if not isinstance(schema, dict):
        raise SchemaError(f"{path} 的 schema 不是对象")
    if "enum" in schema and instance not in schema["enum"]:
        raise SchemaError(f"{path} 不在 enum 中")
    expected = schema.get("type")
    if expected is not None and not _type_matches(instance, expected):
        raise SchemaError(f"{path} 类型应为 {expected}")
    types = expected if isinstance(expected, list) else [expected]
    if instance is None:
        return
    if "object" in types and isinstance(instance, dict):
        _assert_object(instance, schema, path)
    if "array" in types and isinstance(instance, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(instance):
                _assert_schema(item, items, f"{path}[{index}]")


def _assert_object(instance: dict[str, Any], schema: dict[str, Any], path: str) -> None:
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    additional = schema.get("additionalProperties", True)
    for key in required:
        if key not in instance:
            raise SchemaError(f"{path}.{key} 缺失")
    for key, value in instance.items():
        if key in properties:
            _assert_schema(value, properties[key], f"{path}.{key}")
        elif additional is False:
            raise SchemaError(f"{path}.{key} 不允许")
        elif isinstance(additional, dict):
            _assert_schema(value, additional, f"{path}.{key}")


def _type_matches(instance: Any, expected: str | list[str]) -> bool:
    options = expected if isinstance(expected, list) else [expected]
    return any(_one_type(instance, name) for name in options)


def _one_type(instance: Any, name: str) -> bool:
    if name == "object":
        return isinstance(instance, dict)
    if name == "array":
        return isinstance(instance, list)
    if name == "string":
        return isinstance(instance, str)
    if name == "boolean":
        return isinstance(instance, bool)
    if name == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if name == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if name == "null":
        return instance is None
    return True
