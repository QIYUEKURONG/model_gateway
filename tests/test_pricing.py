from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from model_gateway import ErrorCode, GatewayError, TokenUsage, quote, supported_models
from model_gateway.pricing import is_deepseek_peak


def test_supported_models_cover_both_providers():
    assert "deepseek-v4-flash" in supported_models("deepseek")
    assert "deepseek-flash" in supported_models("deepseek")
    assert "claude-sonnet-5-5" in supported_models("anthropic")
    with pytest.raises(GatewayError) as caught:
        supported_models("other")
    assert caught.value.code == ErrorCode.INVALID_REQUEST


def test_deepseek_peak_windows_and_holidays():
    holiday = date(2026, 10, 28)
    assert is_deepseek_peak(datetime(2026, 10, 28, 1, 0, tzinfo=timezone.utc))
    assert is_deepseek_peak(datetime(2026, 10, 28, 3, 59, tzinfo=timezone.utc))
    assert not is_deepseek_peak(datetime(2026, 10, 28, 4, 0, tzinfo=timezone.utc))
    assert is_deepseek_peak(datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc))
    assert not is_deepseek_peak(datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc))
    assert not is_deepseek_peak(datetime(2026, 10, 31, 2, 0, tzinfo=timezone.utc))
    assert not is_deepseek_peak(datetime(2026, 10, 28, 2, 0, tzinfo=timezone.utc), {holiday})
    assert not is_deepseek_peak(datetime(2026, 10, 28, 10, 0))


def test_deepseek_flash_off_peak_and_peak_cost():
    usage = TokenUsage(
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=0,
        uncached_input_tokens=1000,
        total_tokens=1500,
    )
    off = quote("deepseek", "deepseek-v4-flash", usage, at=datetime(2026, 10, 28, 12, tzinfo=timezone.utc))
    assert off.price_tier == "off_peak"
    assert off.input_cost == Decimal("0.00015")
    assert off.output_cost == Decimal("0.0003")
    assert off.total_cost == Decimal("0.00045")

    hit = TokenUsage(
        input_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        uncached_input_tokens=0,
        output_tokens=0,
        total_tokens=1_000_000,
    )
    peak = quote("deepseek", "deepseek-flash", hit, at=datetime(2026, 10, 28, 2, tzinfo=timezone.utc))
    assert peak.price_tier == "peak"
    assert peak.cache_read_cost == Decimal("0.006")
    assert peak.input_cost == Decimal("0")


def test_anthropic_cache_and_haiku_threshold():
    usage = TokenUsage(
        input_tokens=1200,
        output_tokens=500,
        cache_read_tokens=100,
        cache_write_tokens=100,
        cache_write_5m_tokens=100,
        uncached_input_tokens=1000,
        total_tokens=1700,
    )
    cost = quote("anthropic", "claude-sonnet-5-5", usage)
    assert cost.price_tier == "standard"
    assert cost.input_cost == Decimal("0.002")
    assert cost.output_cost == Decimal("0.005")
    assert cost.cache_read_cost == Decimal("0.00001")
    assert cost.cache_write_cost == Decimal("0.00025")

    low = quote(
        "anthropic",
        "claude-haiku-5-5",
        TokenUsage(input_tokens=100_000, uncached_input_tokens=100_000, total_tokens=100_000),
    )
    high = quote(
        "anthropic",
        "claude-haiku-5-5",
        TokenUsage(input_tokens=100_001, uncached_input_tokens=100_001, total_tokens=100_001),
    )
    assert low.price_tier == "standard"
    assert low.input_cost == Decimal("0.01")
    assert high.price_tier == "standard_long"
    assert high.input_cost == Decimal("0.0500005")


def test_unknown_model_is_rejected():
    with pytest.raises(GatewayError) as caught:
        quote("deepseek", "deepseek-v9", TokenUsage())
    assert caught.value.code == ErrorCode.INVALID_REQUEST
