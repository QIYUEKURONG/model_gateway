"""指数退避和令牌桶限流。时钟与睡眠函数可注入，测试不必真的等待。"""

from __future__ import annotations

import random
import threading
from typing import Any, Callable

from model_gateway.errors import GatewayError
from model_gateway.models import ChatMessage, RateLimitPolicy, RetryPolicy


class ManualClock:
    """单调时钟。__call__ 读当前秒数，sleep 与 advance 向前拨。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("不能把时钟往回拨")
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("睡眠时间不能为负")
        self.advance(seconds)


class TokenBucket:
    """令牌桶，开始时是满的。取不到就睡到够用，睡眠函数由调用方注入。"""
    def __init__(
        self,
        rate_per_second: float,
        capacity: float,
        clock: Callable[[], float],
        sleeper: Callable[[float], None],
    ) -> None:
        if rate_per_second <= 0 or capacity <= 0:
            raise ValueError("限流速率和容量必须大于 0")
        self.rate = rate_per_second
        self.capacity = capacity
        self.tokens = capacity
        self.updated = clock()
        self.clock = clock
        self.sleeper = sleeper
        self._lock = threading.Lock()

    def acquire(self, amount: float = 1) -> None:
        if amount <= 0:
            return
        if amount > self.capacity:
            raise ValueError("单次获取量超过限流桶容量")
        while True:
            with self._lock:
                self._refill()
                if self.tokens >= amount:
                    self.tokens -= amount
                    return
                wait = (amount - self.tokens) / self.rate
            self.sleeper(wait)

    def _refill(self) -> None:
        now = self.clock()
        elapsed = now - self.updated
        if elapsed <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.updated = now


class RateLimiter:
    def __init__(
        self,
        policy: RateLimitPolicy | None,
        clock: Callable[[], float],
        sleeper: Callable[[float], None],
    ) -> None:
        self.requests: TokenBucket | None = None
        self.tokens: TokenBucket | None = None
        if policy and policy.requests_per_minute:
            burst = policy.request_burst or policy.requests_per_minute
            self.requests = TokenBucket(policy.requests_per_minute / 60, burst, clock, sleeper)
        if policy and policy.input_tokens_per_minute:
            burst = policy.token_burst or policy.input_tokens_per_minute
            self.tokens = TokenBucket(
                policy.input_tokens_per_minute / 60, burst, clock, sleeper
            )

    def acquire(self, estimated_input_tokens: int = 1) -> None:
        if self.requests is not None:
            self.requests.acquire(1)
        if self.tokens is not None:
            amount = min(float(max(estimated_input_tokens, 1)), self.tokens.capacity)
            self.tokens.acquire(amount)


def estimate_input_tokens(messages: list[ChatMessage]) -> int:
    """限流用的粗估：大约 4 个字符一个 token，至少算 1。"""
    chars = sum(len(message.content) for message in messages)
    return max(1, chars // 4)


def run_with_retry(
    operation: Callable[[int], Any],
    policy: RetryPolicy,
    sleeper: Callable[[float], None],
    rng: random.Random | None = None,
) -> Any:
    """跑到成功或不可重试为止。attempt 从 1 起，传给 operation 记进账本。"""
    generator = rng or random.Random()
    last: GatewayError | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return operation(attempt)
        except GatewayError as exc:
            last = exc
            if attempt >= policy.max_attempts or not exc.retryable or not policy.allows(exc.code):
                raise
            sleeper(
                policy.delay_seconds(attempt, retry_after=exc.retry_after, rng=generator)
            )
    assert last is not None
    raise last
