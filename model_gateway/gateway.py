"""网关门面：渲染提示词、限流、退避、调用供应商，并把花费和时间记进 SQLite。"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator

from model_gateway.errors import ErrorCode, GatewayError
from model_gateway.models import (
    CallRecord,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    CostBreakdown,
    PromptBinding,
    ProviderRequest,
    RateLimitPolicy,
    RenderedPrompt,
    RetryPolicy,
    Role,
    StreamEvent,
    TokenUsage,
)
from model_gateway.pricing import FlatPrice, quote, zero_cost
from model_gateway.prompts import PromptStore
from model_gateway.providers.anthropic import AnthropicProvider
from model_gateway.providers.base import ProviderResult
from model_gateway.providers.deepseek import DeepSeekProvider
from model_gateway.resilience import RateLimiter, estimate_input_tokens, run_with_retry
from model_gateway.sse import encode_sse
from model_gateway.structured import parse_model_output
from model_gateway.usage import UsageStore


class ModelGateway:
    """一次调用的门面。complete() 等整段结果，stream_events() / stream_sse() 按块往外送。

    两条路共用校验、提示词渲染、限流、计价和记账。流式在已经把字交给调用方之后不再重试。
    """
    def __init__(
        self,
        *,
        db_path: str | Path = "gateway.db",
        deepseek_api_key: str | None = None,
        anthropic_api_key: str | None = None,
        deepseek_base_url: str = "https://api.deepseek.com",
        anthropic_base_url: str = "https://api.anthropic.com",
        retry: RetryPolicy | None = None,
        rate_limits: dict[str, RateLimitPolicy] | None = None,
        providers: dict[str, Any] | None = None,
        http_clients: dict[str, Any] | None = None,
        timer: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
        now: Callable[[], datetime] | None = None,
        rng: random.Random | None = None,
        holidays: set[date] | None = None,
        price_overrides: dict[str, FlatPrice] | None = None,
    ) -> None:
        self.prompts = PromptStore(db_path)
        self.usage = UsageStore(db_path)
        self.retry = retry or RetryPolicy()
        self._timer = timer or time.perf_counter
        self._sleeper = sleeper or time.sleep
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._rng = rng or random.Random()
        self.holidays = holidays or set()
        self.price_overrides = price_overrides or {}
        limits = rate_limits or {}
        self._limiters = {
            "deepseek": RateLimiter(limits.get("deepseek"), self._timer, self._sleeper),
            "anthropic": RateLimiter(limits.get("anthropic"), self._timer, self._sleeper),
        }
        if providers is not None:
            self.providers = providers
        else:
            clients = http_clients or {}
            self.providers = {
                "deepseek": DeepSeekProvider(
                    api_key=deepseek_api_key or "",
                    base_url=deepseek_base_url,
                    client=clients.get("deepseek"),
                ),
                "anthropic": AnthropicProvider(
                    api_key=anthropic_api_key or "",
                    base_url=anthropic_base_url,
                    client=clients.get("anthropic"),
                ),
            }

    @classmethod
    def from_env(cls, **overrides: Any) -> "ModelGateway":
        """从进程环境读取密钥。不要把密钥写进源码，再通过参数传进来。"""
        values: dict[str, Any] = {
            "deepseek_api_key": os.environ.get("DEEPSEEK_API_KEY"),
            "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY"),
            "deepseek_base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            "anthropic_base_url": os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            "db_path": os.environ.get("GATEWAY_DB", "gateway.db"),
        }
        values.update(overrides)
        return cls(**values)

    def close(self) -> None:
        self.prompts.close()
        self.usage.close()
        for provider in self.providers.values():
            closer = getattr(provider, "close", None)
            if closer:
                closer()

    def __enter__(self) -> "ModelGateway":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def render_prompt(self, key: str, **kwargs: Any) -> RenderedPrompt:
        return self.prompts.render(key, **kwargs)

    def complete(self, request: ChatRequest | dict[str, Any]) -> ChatResponse:
        """等供应商返回整段正文，校验 JSON 后再交给调用方。可重试错误会在这里退避重来。"""
        request = self._validate(request)
        logical_id = uuid.uuid4().hex
        outer = self._timer()
        provider_name = request.provider
        provider = self._require(provider_name)
        model = request.model or provider.default_model
        meta = _empty_meta()
        try:
            self._ensure_priced(provider_name, model)
            prepared, meta = self._prepare(request, model)
        except GatewayError as exc:
            self._save(
                status="error",
                logical_id=logical_id,
                attempt=1,
                provider=provider_name,
                model=model,
                meta=meta,
                started_at=self._now(),
                latency_ms=self._elapsed_ms(outer),
                usage=TokenUsage(),
                cost=zero_cost(model),
                exc=exc,
                stream=False,
                provider_request_id=None,
                finish_reason=None,
            )
            raise

        def operation(attempt: int) -> ChatResponse:
            self._limiters[provider_name].acquire(estimate_input_tokens(prepared.messages))
            started = self._timer()
            started_at = self._now()
            result: ProviderResult | None = None
            try:
                result = provider.complete(prepared)
                if result.finish_reason == "insufficient_system_resource":
                    raise GatewayError(
                        ErrorCode.OVERLOADED,
                        "供应商资源不足",
                        provider=provider_name,
                        usage=result.usage,
                        provider_request_id=result.id,
                        finish_reason=result.finish_reason,
                    )
                parsed = self._parse(result.content, request, result)
                cost = self._quote_served(provider_name, model, result.model, result.usage)
                saved = self._save(
                    status="success",
                    logical_id=logical_id,
                    attempt=attempt,
                    provider=provider_name,
                    model=result.model or model,
                    meta=meta,
                    started_at=started_at,
                    latency_ms=self._elapsed_ms(started),
                    usage=result.usage,
                    cost=cost,
                    exc=None,
                    stream=False,
                    provider_request_id=result.id,
                    finish_reason=result.finish_reason,
                )
            except GatewayError as exc:
                if result is not None and not isinstance(exc.usage, TokenUsage):
                    exc.usage = result.usage
                    exc.provider_request_id = exc.provider_request_id or result.id
                    exc.finish_reason = exc.finish_reason or result.finish_reason
                self._save_failure(
                    exc,
                    logical_id=logical_id,
                    attempt=attempt,
                    provider=provider_name,
                    model=(result.model if result else model),
                    priced_model=model,
                    meta=meta,
                    started_at=started_at,
                    latency_ms=self._elapsed_ms(started),
                    stream=False,
                )
                raise
            except Exception as exc:
                wrapped = GatewayError(
                    ErrorCode.UNKNOWN,
                    str(exc) or exc.__class__.__name__,
                    provider=provider_name,
                    usage=result.usage if result else None,
                    provider_request_id=result.id if result else None,
                )
                self._save_failure(
                    wrapped,
                    logical_id=logical_id,
                    attempt=attempt,
                    provider=provider_name,
                    model=(result.model if result else model),
                    priced_model=model,
                    meta=meta,
                    started_at=started_at,
                    latency_ms=self._elapsed_ms(started),
                    stream=False,
                )
                raise wrapped from exc
            return _response(
                result=result,
                parsed=parsed,
                cost=cost,
                latency_ms=saved.latency_ms,
                attempt=attempt,
                logical_id=logical_id,
                record_id=saved.id,
                meta=meta,
                provider=provider_name,
            )

        response = run_with_retry(operation, self.retry, self._sleeper, self._rng)
        # 账本里的 latency_ms 是单次尝试；返回值改成含退避和限流等待的端到端时间。
        return response.model_copy(update={"latency_ms": self._elapsed_ms(outer)})

    def stream_events(self, request: ChatRequest | dict[str, Any]) -> Iterator[StreamEvent]:
        """按统一事件往外送。JSON 仍是正文收齐后才解析，结果在 message_stop 的 parsed 里。"""
        request = self._validate(request)
        logical_id = uuid.uuid4().hex
        outer = self._timer()
        provider_name = request.provider
        provider = self._require(provider_name)
        model = request.model or provider.default_model
        meta = _empty_meta()
        try:
            self._ensure_priced(provider_name, model)
            prepared, meta = self._prepare(request, model)
        except GatewayError as exc:
            self._save(
                status="error",
                logical_id=logical_id,
                attempt=1,
                provider=provider_name,
                model=model,
                meta=meta,
                started_at=self._now(),
                latency_ms=self._elapsed_ms(outer),
                usage=TokenUsage(),
                cost=zero_cost(model),
                exc=exc,
                stream=True,
                provider_request_id=None,
                finish_reason=None,
            )
            raise

        for attempt in range(1, self.retry.max_attempts + 1):
            self._limiters[provider_name].acquire(estimate_input_tokens(prepared.messages))
            started = self._timer()
            started_at = self._now()
            emitted = False
            parts: list[str] = []
            usage = TokenUsage()
            finish: str | None = None
            response_id = ""
            seen_model = prepared.model
            try:
                for event in provider.stream(prepared):
                    if event.response_id:
                        response_id = event.response_id
                    if event.model:
                        seen_model = event.model
                    if event.usage is not None:
                        usage = event.usage
                    if event.finish_reason:
                        finish = event.finish_reason
                    if not emitted:
                        emitted = True
                        yield StreamEvent(
                            event="message_start",
                            data={
                                "id": response_id,
                                "provider": provider_name,
                                "model": seen_model,
                                "logical_request_id": logical_id,
                                "attempt": attempt,
                            },
                        )
                    if event.text:
                        parts.append(event.text)
                        yield StreamEvent(event="content_delta", data={"text": event.text})
                if finish == "insufficient_system_resource":
                    raise GatewayError(
                        ErrorCode.OVERLOADED,
                        "供应商资源不足",
                        provider=provider_name,
                        usage=usage,
                        provider_request_id=response_id or None,
                        finish_reason=finish,
                    )
                content = "".join(parts)
                parsed = self._parse(
                    content,
                    request,
                    ProviderResult(response_id, seen_model, content, finish, usage),
                )
                cost = self._quote_served(provider_name, model, seen_model, usage)
                saved = self._save(
                    status="success",
                    logical_id=logical_id,
                    attempt=attempt,
                    provider=provider_name,
                    model=seen_model,
                    meta=meta,
                    started_at=started_at,
                    latency_ms=self._elapsed_ms(started),
                    usage=usage,
                    cost=cost,
                    exc=None,
                    stream=True,
                    provider_request_id=response_id or None,
                    finish_reason=finish,
                )
                yield StreamEvent(
                    event="usage",
                    data=_usage_data(usage, cost, saved.latency_ms, saved.id, logical_id),
                )
                yield StreamEvent(
                    event="message_stop",
                    data={
                        "id": response_id,
                        "content": content,
                        "parsed": parsed,
                        "finish_reason": finish,
                        "attempts": attempt,
                        "logical_request_id": logical_id,
                        "record_id": saved.id,
                        "end_to_end_latency_ms": self._elapsed_ms(outer),
                    },
                )
                return
            except GatewayError as exc:
                saved = self._save_failure(
                    exc if exc.usage is not None else _with_usage(exc, usage),
                    logical_id=logical_id,
                    attempt=attempt,
                    provider=provider_name,
                    model=seen_model,
                    priced_model=model,
                    meta=meta,
                    started_at=started_at,
                    latency_ms=self._elapsed_ms(started),
                    stream=True,
                )
                # 已经向调用方吐过字就不再重试，避免同一段回答发两遍。
                if (
                    emitted
                    or attempt >= self.retry.max_attempts
                    or not exc.retryable
                    or not self.retry.allows(exc.code)
                ):
                    billed = exc.usage if isinstance(exc.usage, TokenUsage) else usage
                    if billed.total_tokens:
                        yield StreamEvent(
                            event="usage",
                            data=_usage_data(
                                billed,
                                CostBreakdown(
                                    input_cost=Decimal("0"),
                                    output_cost=Decimal("0"),
                                    cache_read_cost=Decimal("0"),
                                    cache_write_cost=Decimal("0"),
                                    total_cost=saved.cost_usd,
                                    price_tier=saved.price_tier,
                                    model=saved.model,
                                ),
                                saved.latency_ms,
                                saved.id,
                                logical_id,
                            ),
                        )
                    yield _error_event(exc, saved.id, logical_id)
                    raise
                self._sleeper(
                    self.retry.delay_seconds(attempt, retry_after=exc.retry_after, rng=self._rng)
                )
            except Exception as exc:
                wrapped = GatewayError(
                    ErrorCode.UNKNOWN,
                    str(exc) or exc.__class__.__name__,
                    provider=provider_name,
                )
                saved = self._save_failure(
                    wrapped,
                    logical_id=logical_id,
                    attempt=attempt,
                    provider=provider_name,
                    model=seen_model,
                    priced_model=model,
                    meta=meta,
                    started_at=started_at,
                    latency_ms=self._elapsed_ms(started),
                    stream=True,
                )
                yield _error_event(wrapped, saved.id, logical_id)
                raise wrapped from exc

    def stream_sse(self, request: ChatRequest | dict[str, Any]) -> Iterator[str]:
        """与 stream_events() 同一条路，只是把事件编成 SSE 文本。网页调用走这里。"""
        for event in self.stream_events(request):
            yield encode_sse(event.event, event.data)

    def _validate(self, request: ChatRequest | dict[str, Any]) -> ChatRequest:
        if isinstance(request, ChatRequest):
            return request
        return ChatRequest.model_validate(request)

    def _require(self, name: str) -> Any:
        provider = self.providers.get(name)
        if provider is None:
            raise GatewayError(ErrorCode.INVALID_REQUEST, f"未注册供应商 {name}", provider=name)
        return provider

    def _ensure_priced(self, provider: str, model: str) -> None:
        """发请求前先计价一次。价目表里没有的模型在这里拒绝，避免打完才发现花费无法计算。"""
        self._quote(provider, model, TokenUsage())

    def _quote(self, provider: str, model: str, usage: TokenUsage) -> CostBreakdown:
        return quote(
            provider,
            model,
            usage,
            at=self._now(),
            holidays=self.holidays,
            overrides=self.price_overrides,
        )

    def _quote_served(
        self,
        provider: str,
        requested: str,
        served: str | None,
        usage: TokenUsage,
    ) -> CostBreakdown:
        # 供应商可能回一个不在价目表里的版本号。计价失败时退回请求的模型，响应里的 model 仍保留原文。
        if served and served != requested:
            try:
                return self._quote(provider, served, usage)
            except GatewayError:
                pass
        return self._quote(provider, requested, usage)

    def _prepare(self, request: ChatRequest, model: str) -> tuple[ProviderRequest, dict[str, Any]]:
        """有版本化提示词时，渲染结果插到消息最前面。外部资料附在正文后，不参与变量替换。"""
        messages = list(request.messages)
        meta = _empty_meta()
        if request.prompt is not None:
            rendered = self._render_binding(request.prompt)
            meta = {
                "prompt_key": rendered.prompt_key,
                "prompt_version": rendered.version,
                "materials_json": _material_trace(rendered),
            }
            messages = [
                ChatMessage(role=Role(rendered.role), content=rendered.composed_text()),
                *messages,
            ]
        if not messages:
            raise GatewayError(ErrorCode.INVALID_REQUEST, "渲染后没有任何消息", provider=request.provider)
        prepared = ProviderRequest(
            model=model,
            messages=messages,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            response_format=request.response_format,
        )
        return prepared, meta

    def _render_binding(self, binding: PromptBinding) -> RenderedPrompt:
        return self.prompts.render(
            binding.key,
            version=binding.version,
            variables=binding.variables,
            materials=binding.materials,
        )

    def _parse(self, content: str, request: ChatRequest, result: ProviderResult) -> Any:
        try:
            return parse_model_output(content, request.response_format)
        except GatewayError as exc:
            # 解析失败也要把已经花掉的 token 带出去，账本不能记成 0。
            exc.usage = result.usage
            exc.provider_request_id = result.id
            exc.finish_reason = result.finish_reason
            raise

    def _elapsed_ms(self, started: float) -> float:
        return round((self._timer() - started) * 1000, 3)

    def _save_failure(self, exc: GatewayError, **kwargs: Any) -> CallRecord:
        usage = exc.usage if isinstance(exc.usage, TokenUsage) else TokenUsage()
        served = kwargs["model"]
        requested = kwargs.pop("priced_model", served)
        try:
            cost = self._quote_served(kwargs["provider"], requested, served, usage)
        except GatewayError:
            cost = zero_cost(served)
        return self._save(
            status="error",
            usage=usage,
            cost=cost,
            exc=exc,
            provider_request_id=exc.provider_request_id,
            finish_reason=exc.finish_reason,
            **kwargs,
        )

    def _save(
        self,
        *,
        status: str,
        logical_id: str,
        attempt: int,
        provider: str,
        model: str,
        meta: dict[str, Any],
        started_at: datetime,
        latency_ms: float,
        usage: TokenUsage,
        cost: CostBreakdown,
        exc: GatewayError | None,
        stream: bool,
        provider_request_id: str | None,
        finish_reason: str | None,
    ) -> CallRecord:
        return self.usage.record(
            CallRecord(
                id=uuid.uuid4().hex,
                logical_request_id=logical_id,
                attempt=attempt,
                provider=provider,
                model=model,
                prompt_key=meta.get("prompt_key"),
                prompt_version=meta.get("prompt_version"),
                materials_json=meta.get("materials_json"),
                started_at=started_at,
                latency_ms=latency_ms,
                status=status,  # type: ignore[arg-type]
                error_code=exc.code if exc else None,
                error_message=_clip(exc.message) if exc else None,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                uncached_input_tokens=usage.uncached_input_tokens,
                total_tokens=usage.total_tokens,
                cost_usd=cost.total_cost,
                price_tier=cost.price_tier,
                provider_request_id=provider_request_id,
                stream=stream,
                finish_reason=finish_reason,
            )
        )


def _empty_meta() -> dict[str, Any]:
    return {"prompt_key": None, "prompt_version": None, "materials_json": None}


def _material_trace(rendered: RenderedPrompt) -> str:
    items = []
    for material in rendered.materials:
        item: dict[str, Any] = {"slot": material.name, "title": material.title}
        if material.material_id:
            item["material_id"] = material.material_id
            item["version"] = material.material_version
        else:
            item["inline_sha256"] = hashlib.sha256(material.content.encode()).hexdigest()
            item["chars"] = len(material.content)
        items.append(item)
    return json.dumps(items, ensure_ascii=False)


def _response(
    *,
    result: ProviderResult,
    parsed: Any,
    cost: CostBreakdown,
    latency_ms: float,
    attempt: int,
    logical_id: str,
    record_id: str,
    meta: dict[str, Any],
    provider: str,
) -> ChatResponse:
    return ChatResponse(
        id=result.id or record_id,
        provider=provider,
        model=result.model,
        content=result.content,
        parsed=parsed,
        finish_reason=result.finish_reason,
        usage=result.usage,
        cost=cost,
        latency_ms=latency_ms,
        attempt_latency_ms=latency_ms,
        attempts=attempt,
        logical_request_id=logical_id,
        record_id=record_id,
        prompt_key=meta.get("prompt_key"),
        prompt_version=meta.get("prompt_version"),
    )


def _usage_data(
    usage: TokenUsage,
    cost: CostBreakdown,
    latency_ms: float,
    record_id: str,
    logical_id: str,
) -> dict[str, Any]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "uncached_input_tokens": usage.uncached_input_tokens,
        "total_tokens": usage.total_tokens,
        "cost_usd": format(cost.total_cost, "f"),
        "price_tier": cost.price_tier,
        "latency_ms": latency_ms,
        "record_id": record_id,
        "logical_request_id": logical_id,
    }


def _error_event(exc: GatewayError, record_id: str, logical_id: str) -> StreamEvent:
    return StreamEvent(
        event="error",
        data={
            "code": exc.code.value,
            "message": exc.message,
            "retryable": exc.retryable,
            "record_id": record_id,
            "logical_request_id": logical_id,
        },
    )


def _with_usage(exc: GatewayError, usage: TokenUsage) -> GatewayError:
    exc.usage = usage
    return exc


def _clip(message: str, limit: int = 500) -> str:
    if len(message) <= limit:
        return message
    return message[: limit - 1] + "…"
