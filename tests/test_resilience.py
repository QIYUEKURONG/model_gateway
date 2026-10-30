import pytest

from model_gateway import ErrorCode, GatewayError, ManualClock, RateLimitPolicy, RetryPolicy, run_with_retry
from model_gateway.resilience import RateLimiter, TokenBucket, estimate_input_tokens
from model_gateway import ChatMessage


def test_delay_seconds_exponential_cap_and_retry_after():
    policy = RetryPolicy(
        max_attempts=4,
        base_delay_seconds=0.5,
        multiplier=2,
        max_delay_seconds=1.2,
        jitter_ratio=0,
    )
    rng = __import__("random").Random(0)
    assert policy.delay_seconds(1, retry_after=None, rng=rng) == 0.5
    assert policy.delay_seconds(2, retry_after=None, rng=rng) == 1.0
    assert policy.delay_seconds(3, retry_after=None, rng=rng) == 1.2
    assert policy.delay_seconds(1, retry_after=5, rng=rng) == 5
    assert policy.allows(ErrorCode.RATE_LIMITED)
    assert not policy.allows(ErrorCode.AUTHENTICATION)


def test_run_with_retry_sleeps_then_stops_on_auth():
    clock = ManualClock()
    policy = RetryPolicy(max_attempts=3, base_delay_seconds=0.5, multiplier=2, jitter_ratio=0, max_delay_seconds=10)
    calls = []

    def operation(attempt):
        calls.append(attempt)
        if attempt < 3:
            raise GatewayError(ErrorCode.RATE_LIMITED, "slow")
        return "ok"

    assert run_with_retry(operation, policy, clock.sleep) == "ok"
    assert calls == [1, 2, 3]
    assert clock.now == 1.5

    clock = ManualClock()

    def denied(_attempt):
        raise GatewayError(ErrorCode.AUTHENTICATION, "no")

    with pytest.raises(GatewayError) as caught:
        run_with_retry(denied, policy, clock.sleep)
    assert caught.value.code == ErrorCode.AUTHENTICATION
    assert clock.now == 0


def test_token_bucket_and_rate_limiter():
    clock = ManualClock()
    bucket = TokenBucket(rate_per_second=1, capacity=1, clock=clock, sleeper=clock.sleep)
    bucket.acquire(1)
    bucket.acquire(1)
    assert clock.now == 1
    bucket.acquire(1)
    assert clock.now == 2
    with pytest.raises(ValueError):
        bucket.acquire(2)

    clock = ManualClock()
    limiter = RateLimiter(
        RateLimitPolicy(requests_per_minute=60, request_burst=1, input_tokens_per_minute=60, token_burst=10),
        clock,
        clock.sleep,
    )
    limiter.acquire(4)
    assert clock.now == 0
    limiter.acquire(4)
    assert clock.now == 1


def test_estimate_input_tokens_and_manual_clock():
    assert estimate_input_tokens([ChatMessage(role="user", content="a" * 8)]) == 2
    assert estimate_input_tokens([ChatMessage(role="user", content="a")]) == 1
    clock = ManualClock()
    with pytest.raises(ValueError):
        clock.sleep(-1)
    with pytest.raises(ValueError):
        TokenBucket(0, 1, clock, clock.sleep)
