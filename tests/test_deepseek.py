import json

import httpx
import pytest

from model_gateway import ErrorCode, GatewayError, ProviderRequest, ResponseFormat, ChatMessage
from model_gateway.providers import DeepSeekProvider, usage_from_deepseek
from model_gateway.providers.base import ProviderEvent


def _request(**overrides):
    payload = dict(
        model="deepseek-v4-flash",
        messages=[ChatMessage(role="user", content="Which city? Reply with json.")],
        response_format=ResponseFormat(
            type="json_schema",
            json_schema={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        ),
    )
    payload.update(overrides)
    return ProviderRequest(**payload)


def test_usage_from_deepseek_splits_cache():
    usage = usage_from_deepseek(
        {
            "prompt_tokens": 20,
            "completion_tokens": 5,
            "total_tokens": 25,
            "prompt_cache_hit_tokens": 4,
            "prompt_cache_miss_tokens": 16,
        }
    )
    assert usage.cache_read_tokens == 4
    assert usage.uncached_input_tokens == 16
    assert usage.output_tokens == 5
    cached = usage_from_deepseek(
        {"prompt_tokens": 10, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 6}}
    )
    assert cached.cache_read_tokens == 6
    assert cached.uncached_input_tokens == 4


def test_complete_sends_json_mode_and_reads_usage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"city":"Paris"}'},
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 5,
                    "total_tokens": 25,
                    "prompt_cache_hit_tokens": 4,
                    "prompt_cache_miss_tokens": 16,
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = DeepSeekProvider(api_key="sk-test", client=client)
    result = provider.complete(_request(temperature=0.2, max_tokens=128))
    assert seen["auth"] == "Bearer sk-test"
    assert seen["url"].endswith("/chat/completions")
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["temperature"] == 0.2
    assert seen["body"]["stream"] is False
    assert "json" in seen["body"]["messages"][0]["content"].lower()
    assert result.content == '{"city":"Paris"}'
    assert result.usage.cache_read_tokens == 4
    assert result.usage.uncached_input_tokens == 16
    client.close()


def test_stream_parses_sse_until_done():
    chunks = [
        {
            "id": "chatcmpl-1",
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl-1",
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {"content": '{"city":'}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl-1",
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {"content": '"Paris"}'}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl-1",
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "total_tokens": 14,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 10,
            },
        },
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = DeepSeekProvider(api_key="sk-test", client=client)
    events = [event for event in provider.stream(_request()) if isinstance(event, ProviderEvent)]
    text = "".join(event.text for event in events)
    assert text == '{"city":"Paris"}'
    assert events[-1].finish_reason == "stop"
    assert events[-1].usage is not None
    assert events[-1].usage.output_tokens == 4
    client.close()


def test_rate_limit_and_transport_errors():
    def limited(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"retry-after": "3"},
            json={"error": {"message": "slow down", "type": "rate_limit_error"}},
        )

    client = httpx.Client(transport=httpx.MockTransport(limited))
    provider = DeepSeekProvider(api_key="sk-test", client=client)
    with pytest.raises(GatewayError) as caught:
        provider.complete(_request())
    assert caught.value.code == ErrorCode.RATE_LIMITED
    assert caught.value.retry_after == 3

    def offline(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = httpx.Client(transport=httpx.MockTransport(offline))
    provider = DeepSeekProvider(api_key="sk-test", client=client)
    with pytest.raises(GatewayError) as caught:
        provider.complete(_request())
    assert caught.value.code == ErrorCode.NETWORK_ERROR

    provider = DeepSeekProvider(api_key="", client=httpx.Client(transport=httpx.MockTransport(limited)))
    with pytest.raises(GatewayError) as caught:
        provider.complete(_request())
    assert caught.value.code == ErrorCode.AUTHENTICATION
