from datetime import datetime, timedelta, timezone
from decimal import Decimal

from model_gateway import CallRecord, ErrorCode, UsageStore, summarize


def _record(record_id, logical_id, attempt, status, latency, tokens, when):
    return CallRecord(
        id=record_id,
        logical_request_id=logical_id,
        attempt=attempt,
        provider="deepseek",
        model="deepseek-v4-flash",
        started_at=when,
        latency_ms=latency,
        status=status,
        error_code=None if status == "success" else ErrorCode.RATE_LIMITED,
        input_tokens=tokens,
        output_tokens=tokens // 2,
        uncached_input_tokens=tokens,
        total_tokens=tokens + tokens // 2,
        cost_usd=Decimal("0.00045") if status == "success" else Decimal("0.0001"),
        price_tier="off_peak",
        stream=False,
    )


def test_summarize_separates_attempt_and_request_error_rate():
    start = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    records = [
        _record("a1", "A", 1, "error", 100, 10, start),
        _record("a2", "A", 2, "success", 200, 20, start + timedelta(seconds=1)),
        _record("b1", "B", 1, "error", 300, 30, start + timedelta(seconds=2)),
    ]
    summary = summarize(records)
    assert summary.attempts == 3
    assert summary.requests == 2
    assert summary.successes == 1
    assert summary.errors == 2
    assert summary.error_rate == 2 / 3
    assert summary.request_error_rate == 0.5
    assert summary.input_tokens == 60
    assert summary.total_tokens == 90
    assert summary.cost_usd == Decimal("0.00065")
    assert summary.avg_latency_ms == 200
    assert summary.max_latency_ms == 300
    assert summarize([]).error_rate == 0


def test_usage_store_filters_and_reads_back(tmp_path):
    store = UsageStore(tmp_path / "usage.db")
    start = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    saved = store.record(_record("a1", "A", 1, "error", 100, 10, start))
    store.record(_record("a2", "A", 2, "success", 180, 40, start + timedelta(seconds=1)))
    assert store.get("a1").error_code == ErrorCode.RATE_LIMITED
    assert saved.latency_ms == 100
    assert len(store.list_calls(logical_request_id="A")) == 2
    assert len(store.list_calls(status="success")) == 1
    assert store.list_calls(since=start + timedelta(seconds=1))[0].id == "a2"
    assert store.list_calls(until=start)[0].id == "a1"
    summary = store.summary(provider="deepseek", model="deepseek-v4-flash")
    assert summary.attempts == 2
    assert summary.total_tokens == 75
    assert store.summary(provider="anthropic").attempts == 0
    store.close()
