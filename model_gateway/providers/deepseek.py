"""DeepSeek V4 Flash。走 OpenAI 兼容的 /chat/completions。

默认模型名 deepseek-v4-flash。官方当前也接受 deepseek-flash，
两者都由 Flash 档计价。JSON 模式只保证 json_object，json_schema 在本地再校验一层。
"""

from __future__ import annotations

import json
from typing import Iterator

import httpx

from model_gateway.errors import ErrorCode, GatewayError, map_error_body
from model_gateway.models import ChatMessage, ProviderRequest, Role, TokenUsage
from model_gateway.providers.base import BaseProvider, ProviderEvent, ProviderResult
from model_gateway.sse import parse_sse
from model_gateway.structured import (
    deepseek_response_format,
    ensure_json_word,
    json_schema_instruction,
)


class DeepSeekProvider(BaseProvider):
    name = "deepseek"
    default_model = "deepseek-v4-flash"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.deepseek.com",
        client: httpx.Client | None = None,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(api_key=api_key, base_url=base_url, client=client, timeout=timeout)

    def complete(self, request: ProviderRequest) -> ProviderResult:
        self._require_key()
        response = self._post(self._url(), self._payload(request, stream=False), self._headers())
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise GatewayError(
                ErrorCode.PROVIDER_ERROR,
                "DeepSeek 返回的正文不是 JSON",
                provider=self.name,
            ) from exc
        return _result(data, request.model)

    def stream(self, request: ProviderRequest) -> Iterator[ProviderEvent]:
        self._require_key()
        lines = self._iter_lines(self._url(), self._payload(request, stream=True), self._headers())
        for _event_name, data in parse_sse(lines):
            if data.strip() == "[DONE]":
                break
            try:
                payload = json.loads(data)
            except json.JSONDecodeError as exc:
                raise GatewayError(
                    ErrorCode.PROVIDER_ERROR,
                    f"DeepSeek 的 SSE 不是合法 JSON: {exc}",
                    provider=self.name,
                ) from exc
            if isinstance(payload, dict) and "error" in payload and "choices" not in payload:
                raise map_error_body(payload, self.name)
            event = _event_from_chunk(payload, request.model)
            if event is not None:
                yield event

    def _url(self) -> str:
        if self.base_url.endswith("/v1"):
            return self.base_url + "/chat/completions"
        return self.base_url + "/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _payload(self, request: ProviderRequest, *, stream: bool) -> dict:
        messages = list(request.messages)
        instruction = json_schema_instruction(request.response_format)
        if instruction:
            messages = [ChatMessage(role=Role.SYSTEM, content=instruction), *messages]
        messages = ensure_json_word(messages, request.response_format)
        payload: dict = {
            "model": request.model,
            "messages": [{"role": message.role.value, "content": message.content} for message in messages],
            "stream": stream,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        response_format = deepseek_response_format(request.response_format)
        if response_format is not None:
            payload["response_format"] = response_format
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload


def usage_from_deepseek(raw: dict | None) -> TokenUsage:
    """缓存命中优先读 prompt_cache_hit_tokens，否则读 prompt_tokens_details.cached_tokens。"""
    raw = raw or {}
    prompt = int(raw.get("prompt_tokens") or 0)
    output = int(raw.get("completion_tokens") or 0)
    total = int(raw.get("total_tokens") or (prompt + output))
    if raw.get("prompt_cache_hit_tokens") is not None:
        hit = int(raw["prompt_cache_hit_tokens"])
    else:
        details = raw.get("prompt_tokens_details") or {}
        hit = int(details.get("cached_tokens") or 0)
    if raw.get("prompt_cache_miss_tokens") is not None:
        miss = int(raw["prompt_cache_miss_tokens"])
    else:
        miss = max(prompt - hit, 0)
    return TokenUsage(
        input_tokens=prompt,
        output_tokens=output,
        cache_read_tokens=hit,
        cache_write_tokens=0,
        uncached_input_tokens=miss,
        total_tokens=total,
    )


def _result(data: dict, fallback_model: str) -> ProviderResult:
    choices = data.get("choices") or []
    if not choices:
        raise GatewayError(ErrorCode.PROVIDER_ERROR, "DeepSeek 没有返回 choices", provider="deepseek")
    choice = choices[0]
    message = choice.get("message") or {}
    return ProviderResult(
        id=str(data.get("id") or ""),
        model=str(data.get("model") or fallback_model),
        content=message.get("content") or "",
        finish_reason=choice.get("finish_reason"),
        usage=usage_from_deepseek(data.get("usage")),
    )


def _event_from_chunk(payload: dict, fallback_model: str) -> ProviderEvent | None:
    choices = payload.get("choices") or []
    usage = usage_from_deepseek(payload["usage"]) if payload.get("usage") else None
    text = ""
    finish = None
    if choices:
        choice = choices[0]
        delta = choice.get("delta") or {}
        text = delta.get("content") or ""
        finish = choice.get("finish_reason")
    response_id = str(payload.get("id") or "") or None
    # 只有 id、还没有正文的块也要留下，message_start 才能带上这次调用的 id。
    if not text and usage is None and finish is None and response_id is None:
        return None
    return ProviderEvent(
        text=text,
        usage=usage,
        finish_reason=finish,
        response_id=response_id,
        model=str(payload.get("model") or fallback_model),
    )
