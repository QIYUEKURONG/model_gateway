import json

import httpx
import pytest

from model_gateway import ChatMessage, ErrorCode, GatewayError, ProviderRequest, ResponseFormat
from model_gateway.providers import AnthropicProvider, usage_from_anthropic


def _request():
    return ProviderRequest(
        model="claude-sonnet-5-5",
        messages=[
            ChatMessage(role="system", content="You extract trips."),
            ChatMessage(role="system", content="Be brief."),
            ChatMessage(role="user", content="Paris for 3 nights"),
            ChatMessage(role="user", content="Answer in json."),
        ],
        response_format=ResponseFormat(
            type="json_schema",
            json_schema={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
                "additionalProperties": False,
            },
        ),
        max_tokens=64,
    )


def test_usage_from_anthropic_splits_cache_write():
    usage = usage_from_anthropic(
        {
            "input_tokens": 10,
            "output_tokens": 4,
            "cache_read_input_tokens": 6,
            "cache_creation": {"ephemeral_5m_input_tokens": 2, "ephemeral_1h_input_tokens": 3},
        }
    )
    assert usage.uncached_input_tokens == 10
    assert usage.cache_read_tokens == 6
    assert usage.cache_write_5m_tokens == 2
    assert usage.cache_write_1h_tokens == 3
    assert usage.input_tokens == 21


def test_complete_uses_messages_api_and_output_config():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers["x-api-key"]
        seen["version"] = request.headers["anthropic-version"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "model": "claude-sonnet-5-5",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": '{"city":"Paris"}'}],
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 6,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = AnthropicProvider(api_key="sk-ant", client=client)
    result = provider.complete(_request())
    assert seen["url"].endswith("/v1/messages")
    assert seen["key"] == "sk-ant"
    assert seen["version"] == "2023-06-01"
    assert seen["body"]["system"] == "You extract trips.\n\nBe brief."
    assert seen["body"]["messages"] == [{"role": "user", "content": "Paris for 3 nights\n\nAnswer in json."}]
    assert seen["body"]["max_tokens"] == 64
    assert seen["body"]["output_config"]["format"]["type"] == "json_schema"
    assert "response_format" not in seen["body"]
    assert result.content == '{"city":"Paris"}'
    assert result.usage.output_tokens == 6
    assert result.finish_reason == "end_turn"
    client.close()


def test_stream_accumulates_text_and_usage():
    events = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "model": "claude-sonnet-5-5",
                    "role": "assistant",
                    "content": [],
                    "usage": {"input_tokens": 12, "output_tokens": 1},
                },
            },
        ),
        (
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": '{"city":'}},
        ),
        (
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": '"Paris"}'}},
        ),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 6},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    body = "".join(f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = AnthropicProvider(api_key="sk-ant", client=client)
    collected = list(provider.stream(_request()))
    assert "".join(event.text for event in collected) == '{"city":"Paris"}'
    assert collected[-1].finish_reason == "end_turn" or any(event.finish_reason == "end_turn" for event in collected)
    final = collected[-1]
    assert final.usage is not None
    assert final.usage.uncached_input_tokens == 12
    assert final.usage.output_tokens == 6
    client.close()


def test_requires_user_message_and_maps_stream_error():
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})))
    provider = AnthropicProvider(api_key="sk-ant", client=client)
    with pytest.raises(GatewayError) as caught:
        provider.complete(
            ProviderRequest(model="claude-sonnet-5-5", messages=[ChatMessage(role="system", content="only system")])
        )
    assert caught.value.code == ErrorCode.INVALID_REQUEST

    def overloaded(_request: httpx.Request) -> httpx.Response:
        payload = "event: error\ndata: {\"type\":\"error\",\"error\":{\"type\":\"overloaded_error\",\"message\":\"Overloaded\"}}\n\n"
        return httpx.Response(200, content=payload.encode())

    client = httpx.Client(transport=httpx.MockTransport(overloaded))
    provider = AnthropicProvider(api_key="sk-ant", client=client)
    with pytest.raises(GatewayError) as caught:
        list(provider.stream(_request()))
    assert caught.value.code == ErrorCode.OVERLOADED
