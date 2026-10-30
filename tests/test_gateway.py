import json
from decimal import Decimal

import httpx
import pytest

from model_gateway import (
    ChatMessage,
    ChatRequest,
    ErrorCode,
    GatewayError,
    ManualClock,
    MaterialInput,
    MaterialSpec,
    PromptBinding,
    PromptSlot,
    PromptSpec,
    RateLimitPolicy,
    ResponseFormat,
    RetryPolicy,
    Role,
    SlotKind,
    decode_sse,
)
from model_gateway.gateway import ModelGateway
from model_gateway.providers.base import ProviderEvent, ProviderResult
from model_gateway import TokenUsage

from tests.conftest import OFF_PEAK


class FakeProvider:
    def __init__(self, name, results, clock=None):
        self.name = name
        self.default_model = "deepseek-v4-flash" if name == "deepseek" else "claude-sonnet-5-5"
        self.results = list(results)
        self.clock = clock
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        if self.clock is not None:
            self.clock.advance(0.2)
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def stream(self, request):
        self.requests.append(request)
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        if self.clock is not None:
            self.clock.advance(0.2)
        yield from item

    def close(self):
        return None


def _usage():
    return TokenUsage(
        input_tokens=1000,
        output_tokens=500,
        uncached_input_tokens=1000,
        total_tokens=1500,
    )


def _result(content='{"city":"Paris"}'):
    return ProviderResult(
        id="chatcmpl-1",
        model="deepseek-v4-flash",
        content=content,
        finish_reason="stop",
        usage=_usage(),
    )


def _gateway(tmp_path, provider, clock=None, **overrides):
    clock = clock or ManualClock()
    values = dict(
        db_path=tmp_path / "gateway.db",
        providers={"deepseek": provider, "anthropic": provider},
        timer=clock,
        sleeper=clock.sleep,
        now=lambda: OFF_PEAK,
        retry=RetryPolicy(max_attempts=3, base_delay_seconds=0.5, multiplier=2, jitter_ratio=0, max_delay_seconds=10),
    )
    values.update(overrides)
    return ModelGateway(**values), clock


def test_complete_renders_prompt_and_records_cost(tmp_path):
    clock = ManualClock()
    fake = FakeProvider("deepseek", [_result()], clock)
    gateway, _clock = _gateway(tmp_path, fake, clock)
    gateway.prompts.publish(
        PromptSpec(
            key="trip.plan",
            body="Plan {city}.",
            slots=[
                PromptSlot(name="city", kind=SlotKind.VARIABLE),
                PromptSlot(name="policy", kind=SlotKind.EXTERNAL),
            ],
        )
    )
    gateway.prompts.put_material(MaterialSpec(material_id="refund", title="退改", content="原文 {city} 不可替换"))
    response = gateway.complete(
        {
            "provider": "deepseek",
            "prompt": {
                "key": "trip.plan",
                "variables": {"city": "Paris"},
                "materials": [{"slot": "policy", "material_id": "refund"}],
            },
            "messages": [{"role": "user", "content": "go"}],
            "response_format": {"type": "json_object"},
        }
    )
    assert response.parsed == {"city": "Paris"}
    assert response.cost.total_cost == Decimal("0.00045")
    assert response.latency_ms == 200
    assert response.attempt_latency_ms == 200
    assert response.prompt_version == 1
    sent = fake.requests[0].messages[0].content
    assert "Plan Paris." in sent
    assert "原文 {city} 不可替换" in sent
    row = gateway.usage.get(response.record_id)
    assert row.status == "success"
    assert row.total_tokens == 1500
    assert row.cost_usd == Decimal("0.00045")
    assert row.latency_ms == 200
    assert "refund" in row.materials_json
    assert "原文 {city} 不可替换" not in row.materials_json
    summary = gateway.usage.summary(provider="deepseek")
    assert summary.attempts == 1
    assert summary.error_rate == 0
    assert summary.cost_usd == Decimal("0.00045")
    gateway.close()


def test_retry_records_each_attempt_and_end_to_end_latency(tmp_path):
    clock = ManualClock()
    fake = FakeProvider(
        "deepseek",
        [GatewayError(ErrorCode.RATE_LIMITED, "slow", retry_after=None), _result("ok")],
        clock,
    )
    gateway, _clock = _gateway(tmp_path, fake, clock)
    response = gateway.complete(
        ChatRequest(provider="deepseek", messages=[ChatMessage(role=Role.USER, content="hi")])
    )
    assert response.content == "ok"
    assert response.attempts == 2
    assert response.latency_ms == 900
    assert response.attempt_latency_ms == 200
    rows = gateway.usage.list_calls(logical_request_id=response.logical_request_id)
    assert [row.status for row in rows] == ["error", "success"]
    assert rows[0].error_code == ErrorCode.RATE_LIMITED
    assert gateway.usage.summary().error_rate == 0.5
    assert gateway.usage.summary().request_error_rate == 0
    gateway.close()


def test_served_model_id_falls_back_to_requested_price(tmp_path):
    served = _result("ok")
    served.model = "DeepSeek-V4.1-Flash-1017"
    fake = FakeProvider("deepseek", [served])
    gateway, _clock = _gateway(tmp_path, fake)
    response = gateway.complete(
        ChatRequest(provider="deepseek", messages=[ChatMessage(role=Role.USER, content="hi")])
    )
    assert response.model == "DeepSeek-V4.1-Flash-1017"
    assert response.cost.total_cost == Decimal("0.00045")
    assert response.cost.price_tier == "off_peak"
    gateway.close()


def test_stream_sse_and_structured_output(tmp_path):
    events = [
        ProviderEvent(text='{"ok":', response_id="msg_1", model="deepseek-v4-flash"),
        ProviderEvent(text=" true}", usage=_usage(), finish_reason="stop", response_id="msg_1", model="deepseek-v4-flash"),
    ]
    fake = FakeProvider("deepseek", [events])
    gateway, _clock = _gateway(tmp_path, fake)
    frames = list(
        gateway.stream_sse(
            ChatRequest(
                provider="deepseek",
                messages=[ChatMessage(role="user", content="reply json")],
                response_format=ResponseFormat(type="json_object"),
            )
        )
    )
    decoded = [decode_sse(frame) for frame in frames]
    assert [item[0] for item in decoded] == ["message_start", "content_delta", "content_delta", "usage", "message_stop"]
    assert decoded[-1][1]["parsed"] == {"ok": True}
    assert Decimal(decoded[-2][1]["cost_usd"]) == Decimal("0.00045")
    assert decoded[-2][1]["latency_ms"] == 0
    row = gateway.usage.list_calls()[0]
    assert row.stream is True
    assert row.output_tokens == 500
    gateway.close()


def test_bad_json_records_spend_and_emits_error(tmp_path):
    events = [
        ProviderEvent(text="not-json", usage=_usage(), finish_reason="stop", response_id="msg_2", model="deepseek-v4-flash"),
    ]
    fake = FakeProvider("deepseek", [events])
    gateway, _clock = _gateway(tmp_path, fake)
    frames = []
    with pytest.raises(GatewayError) as caught:
        for frame in gateway.stream_sse(
            ChatRequest(
                provider="deepseek",
                messages=[ChatMessage(role="user", content="reply json")],
                response_format=ResponseFormat(type="json_object"),
            )
        ):
            frames.append(frame)
    assert caught.value.code == ErrorCode.PARSE_ERROR
    names = [decode_sse(frame)[0] for frame in frames]
    assert names[-2:] == ["usage", "error"]
    row = gateway.usage.list_calls()[0]
    assert row.status == "error"
    assert row.error_code == ErrorCode.PARSE_ERROR
    assert row.total_tokens == 1500
    assert row.cost_usd == Decimal("0.00045")
    gateway.close()


def test_validation_unknown_model_missing_material_and_rate_limit(tmp_path):
    fake = FakeProvider("deepseek", [_result("ok"), _result("ok")])
    clock = ManualClock()
    gateway, _clock = _gateway(
        tmp_path,
        fake,
        clock,
        rate_limits={"deepseek": RateLimitPolicy(requests_per_minute=60, request_burst=1)},
    )
    with pytest.raises(Exception):
        gateway.complete({"provider": "deepseek", "temperature": 9})
    assert gateway.usage.summary().attempts == 0

    with pytest.raises(GatewayError) as caught:
        gateway.complete(ChatRequest(provider="deepseek", model="missing-model", messages=[ChatMessage(role="user", content="hi")]))
    assert caught.value.code == ErrorCode.INVALID_REQUEST
    assert fake.requests == []

    gateway.prompts.publish(
        PromptSpec(
            key="needs.doc",
            body="Use the doc.",
            slots=[PromptSlot(name="doc", kind=SlotKind.EXTERNAL)],
        )
    )
    with pytest.raises(GatewayError):
        gateway.complete(
            ChatRequest(
                provider="deepseek",
                prompt=PromptBinding(key="needs.doc"),
                messages=[ChatMessage(role="user", content="hi")],
            )
        )
    assert fake.requests == []

    gateway.complete(ChatRequest(provider="deepseek", messages=[ChatMessage(role="user", content="a" * 8)]))
    gateway.complete(ChatRequest(provider="deepseek", messages=[ChatMessage(role="user", content="b" * 8)]))
    assert clock.now == 1
    rendered = gateway.render_prompt("needs.doc", materials=[MaterialInput(slot="doc", content="附件正文")])
    assert "附件正文" in rendered.composed_text()
    assert "附件正文" not in rendered.text
    gateway.close()


def test_gateway_deepseek_and_anthropic_over_http(tmp_path):
    def deepseek(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("stream"):
            chunks = [
                {"id": "c1", "model": "deepseek-v4-flash", "choices": [{"index": 0, "delta": {"content": '{"city":'}, "finish_reason": None}]},
                {
                    "id": "c1",
                    "model": "deepseek-v4-flash",
                    "choices": [{"index": 0, "delta": {"content": '"Paris"}'}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 1000,
                        "completion_tokens": 500,
                        "total_tokens": 1500,
                        "prompt_cache_hit_tokens": 0,
                        "prompt_cache_miss_tokens": 1000,
                    },
                },
            ]
            raw = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, content=raw.encode())
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "model": "deepseek-v4-flash",
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"city":"Paris"}'}}],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                    "total_tokens": 1500,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 1000,
                },
            },
        )

    def anthropic(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["output_config"]["format"]["type"] == "json_schema"
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "model": "claude-sonnet-5-5",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": '{"city":"Lyon"}'}],
                "usage": {"input_tokens": 20, "output_tokens": 8},
            },
        )

    deepseek_client = httpx.Client(transport=httpx.MockTransport(deepseek))
    anthropic_client = httpx.Client(transport=httpx.MockTransport(anthropic))
    gateway = ModelGateway(
        db_path=tmp_path / "http.db",
        deepseek_api_key="sk-ds",
        anthropic_api_key="sk-ant",
        http_clients={"deepseek": deepseek_client, "anthropic": anthropic_client},
        now=lambda: OFF_PEAK,
        retry=RetryPolicy(max_attempts=1, jitter_ratio=0),
    )
    completed = gateway.complete(
        ChatRequest(
            provider="deepseek",
            messages=[ChatMessage(role="user", content="json please")],
            response_format=ResponseFormat(
                type="json_schema",
                json_schema={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"], "additionalProperties": False},
            ),
        )
    )
    assert completed.parsed == {"city": "Paris"}
    assert completed.cost.total_cost == Decimal("0.00045")
    assert completed.usage.total_tokens == 1500
    frames = list(
        gateway.stream_sse(
            ChatRequest(
                provider="deepseek",
                messages=[ChatMessage(role="user", content="json please")],
                response_format=ResponseFormat(type="json_object"),
            )
        )
    )
    stop = decode_sse(frames[-1])[1]
    assert stop["parsed"] == {"city": "Paris"}
    anthropic_response = gateway.complete(
        ChatRequest(
            provider="anthropic",
            messages=[ChatMessage(role="user", content="json city")],
            response_format=ResponseFormat(
                type="json_schema",
                json_schema={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"], "additionalProperties": False},
            ),
        )
    )
    assert anthropic_response.parsed == {"city": "Lyon"}
    assert anthropic_response.usage.output_tokens == 8
    assert anthropic_response.cost.total_cost > 0
    rows = gateway.usage.list_calls()
    assert len(rows) == 3
    assert all(row.latency_ms >= 0 for row in rows)
    gateway.close()
