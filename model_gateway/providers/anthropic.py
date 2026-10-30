"""Anthropic Messages API。

结构化输出用 output_config.format（json_schema）。
流式事件按官方 SSE：message_start、content_block_delta、message_delta、message_stop。
"""

from __future__ import annotations

import json
from typing import Iterator

import httpx

from model_gateway.errors import ErrorCode, GatewayError, map_error_body
from model_gateway.models import ChatMessage, ProviderRequest, Role, TokenUsage
from model_gateway.providers.base import BaseProvider, ProviderEvent, ProviderResult
from model_gateway.sse import parse_sse
from model_gateway.structured import anthropic_output_config


class AnthropicProvider(BaseProvider):
    name = "anthropic"
    default_model = "claude-sonnet-5-5"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.anthropic.com",
        version: str = "2023-06-01",
        client: httpx.Client | None = None,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(api_key=api_key, base_url=base_url, client=client, timeout=timeout)
        self.version = version

    def complete(self, request: ProviderRequest) -> ProviderResult:
        self._require_key()
        response = self._post(self._url(), self._payload(request, stream=False), self._headers())
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise GatewayError(
                ErrorCode.PROVIDER_ERROR,
                "Anthropic 返回的正文不是 JSON",
                provider=self.name,
            ) from exc
        return _result(data, request.model)

    def stream(self, request: ProviderRequest) -> Iterator[ProviderEvent]:
        self._require_key()
        lines = self._iter_lines(self._url(), self._payload(request, stream=True), self._headers())
        response_id = ""
        model = request.model
        input_usage = TokenUsage()
        for event_name, data in parse_sse(lines):
            try:
                payload = json.loads(data)
            except json.JSONDecodeError as exc:
                raise GatewayError(
                    ErrorCode.PROVIDER_ERROR,
                    f"Anthropic 的 SSE 不是合法 JSON: {exc}",
                    provider=self.name,
                ) from exc
            if event_name == "error" or payload.get("type") == "error":
                raise map_error_body(payload, self.name)
            kind = payload.get("type") or event_name
            if kind == "message_start":
                message = payload.get("message") or {}
                response_id = str(message.get("id") or response_id)
                model = str(message.get("model") or model)
                input_usage = usage_from_anthropic(message.get("usage"))
                yield ProviderEvent(response_id=response_id or None, model=model, usage=input_usage)
            elif kind == "content_block_delta":
                delta = payload.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    yield ProviderEvent(
                        text=delta["text"],
                        response_id=response_id or None,
                        model=model,
                    )
            elif kind == "message_delta":
                output_usage = usage_from_anthropic(payload.get("usage"))
                merged = _merge_usage(input_usage, output_usage)
                input_usage = merged
                finish = (payload.get("delta") or {}).get("stop_reason")
                yield ProviderEvent(
                    usage=merged,
                    finish_reason=finish,
                    response_id=response_id or None,
                    model=model,
                )
            elif kind == "message_stop":
                yield ProviderEvent(
                    usage=input_usage,
                    response_id=response_id or None,
                    model=model,
                )

    def _url(self) -> str:
        if self.base_url.endswith("/v1"):
            return self.base_url + "/messages"
        return self.base_url + "/v1/messages"

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": self.version,
            "content-type": "application/json",
        }

    def _payload(self, request: ProviderRequest, *, stream: bool) -> dict:
        system, messages = _split_messages(request.messages)
        payload: dict = {
            "model": request.model,
            "max_tokens": request.max_tokens or 1024,
            "messages": messages,
            "stream": stream,
        }
        if system:
            payload["system"] = system
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        output_config = anthropic_output_config(request.response_format)
        if output_config is not None:
            payload["output_config"] = output_config
        return payload


def usage_from_anthropic(raw: dict | None) -> TokenUsage:
    """没分开 5 分钟和 1 小时的缓存写入，整段按 5 分钟档计费。"""
    raw = raw or {}
    uncached = int(raw.get("input_tokens") or 0)
    output = int(raw.get("output_tokens") or 0)
    cache_read = int(raw.get("cache_read_input_tokens") or 0)
    creation = raw.get("cache_creation") or {}
    write_5m = int(creation.get("ephemeral_5m_input_tokens") or 0)
    write_1h = int(creation.get("ephemeral_1h_input_tokens") or 0)
    if write_5m or write_1h:
        cache_write = write_5m + write_1h
    else:
        cache_write = int(raw.get("cache_creation_input_tokens") or 0)
        write_5m = cache_write
        write_1h = 0
    return TokenUsage(
        input_tokens=uncached + cache_read + cache_write,
        output_tokens=output,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
        uncached_input_tokens=uncached,
        total_tokens=uncached + cache_read + cache_write + output,
    )


def _merge_usage(base: TokenUsage, delta: TokenUsage) -> TokenUsage:
    """message_delta 里的 output_tokens 是累计值，输入侧沿用 message_start。"""
    output = delta.output_tokens or base.output_tokens
    uncached = base.uncached_input_tokens or delta.uncached_input_tokens
    cache_read = base.cache_read_tokens or delta.cache_read_tokens
    cache_write = base.cache_write_tokens or delta.cache_write_tokens
    write_5m = base.cache_write_5m_tokens or delta.cache_write_5m_tokens
    write_1h = base.cache_write_1h_tokens or delta.cache_write_1h_tokens
    return TokenUsage(
        input_tokens=uncached + cache_read + cache_write,
        output_tokens=output,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
        uncached_input_tokens=uncached,
        total_tokens=uncached + cache_read + cache_write + output,
    )


def _split_messages(messages: list[ChatMessage]) -> tuple[str, list[dict[str, str]]]:
    """system 抽到顶层。连续的同角色消息合并，对话必须以 user 开头。"""
    system_parts: list[str] = []
    dialog: list[dict[str, str]] = []
    for message in messages:
        if message.role == Role.SYSTEM:
            system_parts.append(message.content)
            continue
        role = message.role.value
        if dialog and dialog[-1]["role"] == role:
            dialog[-1]["content"] = dialog[-1]["content"] + "\n\n" + message.content
        else:
            dialog.append({"role": role, "content": message.content})
    if not dialog or dialog[0]["role"] != "user":
        raise GatewayError(
            ErrorCode.INVALID_REQUEST,
            "Anthropic 需要至少一条 user 消息，且不能以 assistant 开头",
            provider="anthropic",
        )
    return "\n\n".join(system_parts), dialog


def _result(data: dict, fallback_model: str) -> ProviderResult:
    parts = []
    for block in data.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return ProviderResult(
        id=str(data.get("id") or ""),
        model=str(data.get("model") or fallback_model),
        content="".join(parts),
        finish_reason=data.get("stop_reason"),
        usage=usage_from_anthropic(data.get("usage")),
    )
