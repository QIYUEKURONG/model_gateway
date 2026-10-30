"""花费计算。

价格取自公开文档（查询日 2026-10-28）：
- DeepSeek Flash：https://api-docs.deepseek.com/quick_start/pricing/
  deepseek-flash、deepseek-v4-flash、deepseek-v4-flash-vision-exp 同价。
  高峰为周一至周五 UTC 01:00–04:00 与 06:00–10:00。传入的节假日整天按闲时。
- Anthropic：https://platform.claude.com/docs/en/about-claude/pricing
  未区分 5 分钟 / 1 小时缓存写入时，缓存写入按 5 分钟档计价。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from model_gateway.errors import ErrorCode, GatewayError
from model_gateway.models import CostBreakdown, TokenUsage

_Q = Decimal("0.00000001")
_MILLION = Decimal(1_000_000)


def _d(value: str) -> Decimal:
    return Decimal(value)


def _money(tokens: int, per_million: Decimal) -> Decimal:
    return ((Decimal(tokens) * per_million) / _MILLION).quantize(_Q, rounding=ROUND_HALF_UP)


class FlatPrice:
    def __init__(
        self,
        input_per_million: str,
        output_per_million: str,
        cache_read_per_million: str = "0",
        cache_write_5m_per_million: str = "0",
        cache_write_1h_per_million: str = "0",
    ) -> None:
        self.input_per_million = _d(input_per_million)
        self.output_per_million = _d(output_per_million)
        self.cache_read_per_million = _d(cache_read_per_million)
        self.cache_write_5m_per_million = _d(cache_write_5m_per_million)
        self.cache_write_1h_per_million = _d(cache_write_1h_per_million)


class TieredPrice:
    def __init__(self, *, low: FlatPrice, high: FlatPrice, threshold_tokens: int) -> None:
        self.low = low
        self.high = high
        self.threshold_tokens = threshold_tokens

    def select(self, prompt_tokens: int) -> FlatPrice:
        if prompt_tokens <= self.threshold_tokens:
            return self.low
        return self.high


# DeepSeek Flash，单位：美元 / 百万 tokens。顺序：闲时、高峰。
_FLASH_OFF_PEAK = FlatPrice("0.15", "0.6", "0.003")
_FLASH_PEAK = FlatPrice("0.3", "1.2", "0.006")
FLASH_MODELS = {
    "deepseek-flash",
    "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
}

_ANTHROPIC: dict[str, FlatPrice | TieredPrice] = {
    "claude-sonnet-5-5": FlatPrice("2", "10", "0.10", "2.50", "4"),
    "claude-sonnet-5": FlatPrice("2", "10", "0.20", "2.50", "4"),
    "claude-sonnet-4-6": FlatPrice("3", "15", "0.30", "3.75", "6"),
    "claude-sonnet-4-5": FlatPrice("3", "15", "0.30", "3.75", "6"),
    "claude-haiku-4-5": FlatPrice("1", "5", "0.10", "1.25", "2"),
    "claude-opus-5-5": FlatPrice("4", "20", "0.20", "5", "8"),
    "claude-opus-5": FlatPrice("5", "25", "0.50", "6.25", "10"),
    "claude-haiku-5-5": TieredPrice(
        threshold_tokens=100_000,
        low=FlatPrice("0.10", "0.50", "0.01", "0.125", "0.20"),
        high=FlatPrice("0.50", "2.50", "0.05", "0.625", "1"),
    ),
}


def supported_models(provider: str) -> list[str]:
    if provider == "deepseek":
        return sorted(FLASH_MODELS)
    if provider == "anthropic":
        return sorted(_ANTHROPIC)
    raise GatewayError(ErrorCode.INVALID_REQUEST, f"未知供应商 {provider}", provider=provider)


def is_deepseek_peak(at: datetime, holidays: set[date] | None = None) -> bool:
    """工作日 UTC 01:00–04:00 与 06:00–10:00 为高峰，右端不含。周末和传入的节假日整天按闲时。"""
    moment = _as_utc(at)
    if moment.date() in (holidays or set()):
        return False
    if moment.weekday() >= 5:
        return False
    minutes = moment.hour * 60 + moment.minute
    return (60 <= minutes < 4 * 60) or (6 * 60 <= minutes < 10 * 60)


def quote(
    provider: str,
    model: str,
    usage: TokenUsage,
    *,
    at: datetime | None = None,
    holidays: set[date] | None = None,
    overrides: dict[str, FlatPrice] | None = None,
) -> CostBreakdown:
    """按公开价目把 token 换成美元，精确到 8 位。价目表里没有的模型在这里拒绝，不记成 0。"""
    moment = _as_utc(at or datetime.now(timezone.utc))
    override_key = f"{provider}:{model}"
    if overrides and override_key in overrides:
        price = overrides[override_key]
        tier = "override"
    elif provider == "deepseek":
        if model not in FLASH_MODELS:
            raise _unknown(provider, model)
        peak = is_deepseek_peak(moment, holidays)
        price = _FLASH_PEAK if peak else _FLASH_OFF_PEAK
        tier = "peak" if peak else "off_peak"
    elif provider == "anthropic":
        listed = _ANTHROPIC.get(model)
        if listed is None:
            raise _unknown(provider, model)
        if isinstance(listed, TieredPrice):
            price = listed.select(usage.input_tokens)
            tier = "standard_long" if price is listed.high else "standard"
        else:
            price = listed
            tier = "standard"
    else:
        raise GatewayError(ErrorCode.INVALID_REQUEST, f"未知供应商 {provider}")

    write_5m = usage.cache_write_5m_tokens
    write_1h = usage.cache_write_1h_tokens
    input_cost = _money(usage.uncached_input_tokens, price.input_per_million)
    output_cost = _money(usage.output_tokens, price.output_per_million)
    cache_read_cost = _money(usage.cache_read_tokens, price.cache_read_per_million)
    cache_write_cost = (
        _money(write_5m, price.cache_write_5m_per_million)
        + _money(write_1h, price.cache_write_1h_per_million)
    ).quantize(_Q, rounding=ROUND_HALF_UP)
    total = (input_cost + output_cost + cache_read_cost + cache_write_cost).quantize(
        _Q, rounding=ROUND_HALF_UP
    )
    return CostBreakdown(
        input_cost=input_cost,
        output_cost=output_cost,
        cache_read_cost=cache_read_cost,
        cache_write_cost=cache_write_cost,
        total_cost=total,
        price_tier=tier,
        model=model,
    )


def zero_cost(model: str, tier: str = "unpriced") -> CostBreakdown:
    zero = Decimal("0")
    return CostBreakdown(
        input_cost=zero,
        output_cost=zero,
        cache_read_cost=zero,
        cache_write_cost=zero,
        total_cost=zero,
        price_tier=tier,
        model=model,
    )


def _unknown(provider: str, model: str) -> GatewayError:
    return GatewayError(
        ErrorCode.INVALID_REQUEST,
        f"没有 {provider}/{model} 的价格表，拒绝调用以免把花费记成 0",
        provider=provider,
    )


def _as_utc(at: datetime) -> datetime:
    """没有时区的时间按 UTC 理解，不按本机时区。"""
    if at.tzinfo is None:
        return at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc)
