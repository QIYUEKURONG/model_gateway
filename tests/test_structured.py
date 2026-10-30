import pytest

from model_gateway import ChatMessage, ErrorCode, GatewayError, ResponseFormat, Role
from model_gateway.structured import (
    anthropic_output_config,
    deepseek_response_format,
    ensure_json_word,
    json_schema_instruction,
    parse_model_output,
)


SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "nights": {"type": "integer"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["city", "nights"],
    "additionalProperties": False,
}


def test_provider_format_translation():
    fmt = ResponseFormat(type="json_schema", json_schema=SCHEMA, name="trip")
    assert deepseek_response_format(fmt) == {"type": "json_object"}
    assert deepseek_response_format(ResponseFormat()) is None
    instruction = json_schema_instruction(fmt)
    assert instruction is not None and "json" in instruction and "city" in instruction
    output = anthropic_output_config(fmt)
    assert output["format"]["type"] == "json_schema"
    assert output["format"]["schema"]["required"] == ["city", "nights"]
    loose = anthropic_output_config(ResponseFormat(type="json_object"))
    assert loose["format"]["schema"]["additionalProperties"] is True


def test_ensure_json_word_only_when_missing():
    user = ChatMessage(role=Role.USER, content="Say hello")
    hinted = ensure_json_word([user], ResponseFormat(type="json_object"))
    assert hinted[0].content == "Respond with valid json."
    already = ensure_json_word(
        [ChatMessage(role=Role.USER, content="return json")],
        ResponseFormat(type="json_object"),
    )
    assert len(already) == 1


def test_parse_json_object_fence_and_schema():
    fmt = ResponseFormat(type="json_object")
    assert parse_model_output('```json\n{"ok": true}\n```', fmt) == {"ok": True}
    parsed = parse_model_output('{"city": "Paris", "nights": 3, "tags": ["a"]}', ResponseFormat(type="json_schema", json_schema=SCHEMA))
    assert parsed["city"] == "Paris"
    with pytest.raises(GatewayError) as missing:
        parse_model_output('{"city": "Paris"}', ResponseFormat(type="json_schema", json_schema=SCHEMA))
    assert missing.value.code == ErrorCode.PARSE_ERROR
    with pytest.raises(GatewayError):
        parse_model_output('{"city": "Paris", "nights": 1, "extra": 1}', ResponseFormat(type="json_schema", json_schema=SCHEMA))
    with pytest.raises(GatewayError):
        parse_model_output("not json", fmt)
    with pytest.raises(GatewayError):
        parse_model_output('{"city": "Paris", "nights": 1.5}', ResponseFormat(type="json_schema", json_schema=SCHEMA))
    assert parse_model_output("hello", None) is None
