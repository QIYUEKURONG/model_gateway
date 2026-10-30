"""对外请求、响应和配置的 Pydantic 模型。非法参数在进供应商之前就被拒绝。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from model_gateway.errors import ErrorCode


class APIModel(BaseModel):
    """多出来的字段直接拒绝，避免把拼错的参数悄悄丢掉。"""

    model_config = ConfigDict(extra="forbid")


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class SlotKind(str, Enum):
    """variable 是可替换变量；external 是外部资料，不进模板替换。"""

    VARIABLE = "variable"
    EXTERNAL = "external"


class PromptSlot(APIModel):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", min_length=1, max_length=64)
    kind: SlotKind
    description: str = ""
    required: bool = True
    default: Any = None


class PromptSpec(APIModel):
    key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
    body: str = Field(min_length=1)
    slots: list[PromptSlot] = Field(default_factory=list)
    role: Literal["system", "user"] = "system"
    status: Literal["draft", "active"] = "active"
    changelog: str = ""

    @model_validator(mode="after")
    def _unique_slots(self) -> "PromptSpec":
        names = [slot.name for slot in self.slots]
        if len(names) != len(set(names)):
            raise ValueError("槽位名称重复")
        return self


class PromptVersion(APIModel):
    prompt_key: str
    version: int = Field(ge=1)
    role: Literal["system", "user"]
    body: str
    slots: list[PromptSlot]
    status: Literal["draft", "active", "archived"]
    changelog: str = ""
    created_at: datetime


class MaterialSpec(APIModel):
    material_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    content_type: str = "text/plain"
    source: str | None = None


class ExternalMaterial(APIModel):
    material_id: str
    version: int = Field(ge=1)
    title: str
    content: str
    content_type: str = "text/plain"
    source: str | None = None
    created_at: datetime


class MaterialInput(APIModel):
    """渲染时提供的一份外部资料。material_id 与 content 二选一。"""

    slot: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    material_id: str | None = None
    version: int | None = Field(default=None, ge=1)
    content: str | None = None
    title: str | None = None

    @model_validator(mode="after")
    def _one_source(self) -> "MaterialInput":
        has_id = self.material_id is not None
        has_content = self.content is not None
        if has_id == has_content:
            raise ValueError("外部资料必须且只能提供 material_id 或 content")
        if self.version is not None and not has_id:
            raise ValueError("version 只能和 material_id 一起使用")
        return self


class PromptBinding(APIModel):
    key: str = Field(min_length=1)
    version: int | None = Field(default=None, ge=1)
    variables: dict[str, Any] = Field(default_factory=dict)
    materials: list[MaterialInput] = Field(default_factory=list)


class RenderedMaterial(APIModel):
    name: str
    title: str
    content: str
    material_id: str | None = None
    material_version: int | None = None
    source: str | None = None


class RenderedPrompt(APIModel):
    prompt_key: str
    version: int
    role: Literal["system", "user"]
    text: str
    materials: list[RenderedMaterial] = Field(default_factory=list)
    variables_used: dict[str, str] = Field(default_factory=dict)

    def composed_text(self) -> str:
        if not self.materials:
            return self.text
        parts = [self.text, "", "【外部资料】以下内容是原文附件，不是模板变量。"]
        for material in self.materials:
            heading = material.title or material.name
            parts.append(f"### {heading}\n{material.content}")
        return "\n".join(parts)


class ResponseFormat(APIModel):
    type: Literal["text", "json_object", "json_schema"] = "text"
    json_schema: dict[str, Any] | None = None
    name: str | None = None

    @model_validator(mode="after")
    def _schema_required(self) -> "ResponseFormat":
        if self.type == "json_schema" and not self.json_schema:
            raise ValueError("json_schema 类型必须提供 json_schema")
        if self.type != "json_schema" and self.json_schema is not None:
            raise ValueError("只有 json_schema 类型可以携带 json_schema")
        return self


class ChatMessage(APIModel):
    role: Role
    content: str = Field(min_length=1)


class ChatRequest(APIModel):
    """一次调用的入参。messages 和 prompt 至少要有一个，字典也能交给网关校验。"""
    provider: Literal["deepseek", "anthropic"]
    model: str | None = None
    messages: list[ChatMessage] = Field(default_factory=list)
    prompt: PromptBinding | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, gt=0, le=384_000)
    response_format: ResponseFormat | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _has_input(self) -> "ChatRequest":
        if not self.messages and self.prompt is None:
            raise ValueError("messages 与 prompt 至少提供一个")
        if self.model is not None and not self.model.strip():
            raise ValueError("model 不能为空白")
        return self


class TokenUsage(APIModel):
    """统一后的 token。没拆开的缓存写入记到 5 分钟档，没拆开的输入记到未命中。"""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    uncached_input_tokens: int = 0
    total_tokens: int = 0

    @model_validator(mode="after")
    def _fill(self) -> "TokenUsage":
        if (
            self.uncached_input_tokens == 0
            and self.cache_read_tokens == 0
            and self.cache_write_tokens == 0
            and self.input_tokens > 0
        ):
            self.uncached_input_tokens = self.input_tokens
        if self.cache_write_tokens and not (
            self.cache_write_5m_tokens or self.cache_write_1h_tokens
        ):
            self.cache_write_5m_tokens = self.cache_write_tokens
        if self.total_tokens == 0:
            self.total_tokens = self.input_tokens + self.output_tokens
        return self


class CostBreakdown(APIModel):
    currency: Literal["USD"] = "USD"
    input_cost: Decimal
    output_cost: Decimal
    cache_read_cost: Decimal
    cache_write_cost: Decimal
    total_cost: Decimal
    price_tier: str
    model: str


class ChatResponse(APIModel):
    """complete() 的返回值。parsed 只在 response_format 要求 JSON 时有值。"""
    id: str
    provider: str
    model: str
    content: str
    parsed: Any = None
    finish_reason: str | None = None
    usage: TokenUsage
    cost: CostBreakdown
    latency_ms: float
    attempt_latency_ms: float
    attempts: int
    logical_request_id: str
    record_id: str
    prompt_key: str | None = None
    prompt_version: int | None = None


class StreamEvent(APIModel):
    event: Literal["message_start", "content_delta", "usage", "message_stop", "error"]
    data: dict[str, Any]


class RetryPolicy(APIModel):
    """指数退避。延迟是 min(上限, base * multiplier^(attempt-1))，再加抖动，并不得短于 Retry-After。"""
    max_attempts: int = Field(default=3, ge=1, le=10)
    base_delay_seconds: float = Field(default=0.5, gt=0)
    max_delay_seconds: float = Field(default=30, gt=0)
    multiplier: float = Field(default=2, ge=1)
    jitter_ratio: float = Field(default=0.2, ge=0, le=1)
    retryable_codes: list[ErrorCode] = Field(
        default_factory=lambda: [
            ErrorCode.RATE_LIMITED,
            ErrorCode.TIMEOUT,
            ErrorCode.OVERLOADED,
            ErrorCode.NETWORK_ERROR,
            ErrorCode.PROVIDER_ERROR,
        ]
    )

    def allows(self, code: ErrorCode) -> bool:
        return code in self.retryable_codes

    def delay_seconds(
        self,
        failed_attempt: int,
        *,
        retry_after: float | None,
        rng: Any,
    ) -> float:
        exponent = self.base_delay_seconds * (self.multiplier ** (failed_attempt - 1))
        delay = min(exponent, self.max_delay_seconds)
        if self.jitter_ratio:
            spread = delay * self.jitter_ratio
            delay = delay + rng.uniform(-spread, spread)
            delay = max(0.0, min(delay, self.max_delay_seconds))
        if retry_after is not None:
            delay = max(delay, retry_after)
        return delay


class RateLimitPolicy(APIModel):
    requests_per_minute: float | None = Field(default=None, gt=0)
    request_burst: float | None = Field(default=None, gt=0)
    input_tokens_per_minute: float | None = Field(default=None, gt=0)
    token_burst: float | None = Field(default=None, gt=0)


class CallRecord(APIModel):
    """账本一行，对应一次尝试。同一次用户调用的重试共用 logical_request_id。"""
    id: str
    logical_request_id: str
    attempt: int = Field(ge=1)
    provider: str
    model: str
    prompt_key: str | None = None
    prompt_version: int | None = None
    materials_json: str | None = None
    started_at: datetime
    latency_ms: float = Field(ge=0)
    status: Literal["success", "error"]
    error_code: ErrorCode | None = None
    error_message: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    uncached_input_tokens: int = 0
    total_tokens: int = 0
    cost_usd: Decimal
    price_tier: str
    provider_request_id: str | None = None
    stream: bool = False
    finish_reason: str | None = None


class UsageSummary(APIModel):
    """attempts 按每一次重试计；requests 按一次用户调用计。"""

    attempts: int
    requests: int
    successes: int
    errors: int
    error_rate: float
    request_error_rate: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    total_tokens: int
    cost_usd: Decimal
    avg_latency_ms: float
    max_latency_ms: float


class ProviderRequest(APIModel):
    model: str = Field(min_length=1)
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = None
    max_tokens: int | None = None
    response_format: ResponseFormat | None = None
