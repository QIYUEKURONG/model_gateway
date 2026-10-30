import pytest
from pydantic import ValidationError

from model_gateway import ChatMessage, ChatRequest, MaterialInput, PromptSpec, PromptSlot, ResponseFormat, Role, SlotKind


def test_chat_request_rejects_empty_input():
    with pytest.raises(ValidationError):
        ChatRequest(provider="deepseek")


def test_chat_request_rejects_temperature_and_blank_model():
    with pytest.raises(ValidationError):
        ChatRequest(
            provider="deepseek",
            model="  ",
            messages=[ChatMessage(role=Role.USER, content="hi")],
        )
    with pytest.raises(ValidationError):
        ChatRequest(
            provider="deepseek",
            temperature=2.5,
            messages=[ChatMessage(role=Role.USER, content="hi")],
        )
    with pytest.raises(ValidationError):
        ChatRequest(
            provider="deepseek",
            max_tokens=0,
            messages=[ChatMessage(role=Role.USER, content="hi")],
        )


def test_response_format_requires_schema_only_for_json_schema():
    with pytest.raises(ValidationError):
        ResponseFormat(type="json_schema")
    with pytest.raises(ValidationError):
        ResponseFormat(type="json_object", json_schema={"type": "object"})
    fmt = ResponseFormat(type="json_schema", json_schema={"type": "object"})
    assert fmt.type == "json_schema"


def test_material_input_is_exclusive():
    with pytest.raises(ValidationError):
        MaterialInput(slot="policy", material_id="refund", content="inline")
    with pytest.raises(ValidationError):
        MaterialInput(slot="policy", content="inline", version=1)
    item = MaterialInput(slot="policy", material_id="refund", version=2)
    assert item.version == 2


def test_prompt_spec_rejects_duplicate_slots():
    slot = dict(name="city", kind=SlotKind.VARIABLE, description="")
    with pytest.raises(ValidationError):
        PromptSpec(key="trip", body="go to {city}", slots=[slot, slot])


def test_chat_message_rejects_empty_content():
    with pytest.raises(ValidationError):
        ChatMessage(role="user", content="")
