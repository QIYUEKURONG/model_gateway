from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

from model_gateway import ErrorCode, is_retryable
from model_gateway.errors import map_error_body, map_http_error


def test_retryable_codes():
    assert ErrorCode.RATE_LIMITED.retryable
    assert ErrorCode.TIMEOUT.retryable
    assert ErrorCode.OVERLOADED.retryable
    assert ErrorCode.NETWORK_ERROR.retryable
    assert ErrorCode.PROVIDER_ERROR.retryable
    assert not ErrorCode.AUTHENTICATION.retryable
    assert not ErrorCode.INVALID_REQUEST.retryable
    assert not ErrorCode.PARSE_ERROR.retryable
    assert not ErrorCode.QUOTA_EXCEEDED.retryable
    assert is_retryable(ErrorCode.PROVIDER_ERROR)


def test_map_http_error_prefers_status_for_auth_and_rate_limit():
    limited = map_http_error(
        429,
        {"error": {"type": "rate_limit_error", "message": "slow down"}},
        {"retry-after": "2.5"},
        "deepseek",
    )
    assert limited.code == ErrorCode.RATE_LIMITED
    assert limited.retry_after == 2.5
    assert limited.retryable
    assert limited.provider == "deepseek"

    denied = map_http_error(401, {"error": {"message": "bad key", "type": "authentication_error"}}, None, "anthropic")
    assert denied.code == ErrorCode.AUTHENTICATION
    assert not denied.retryable

    overloaded = map_http_error(529, {"error": {"type": "overloaded_error", "message": "busy"}}, {}, "anthropic")
    assert overloaded.code == ErrorCode.OVERLOADED


def test_map_http_error_reads_body_when_status_is_ambiguous():
    billing = map_http_error(
        400,
        {"error": {"type": "billing_error", "message": "no credit"}},
        None,
        "anthropic",
    )
    assert billing.code == ErrorCode.QUOTA_EXCEEDED
    huge = map_http_error(413, {"error": {"message": "too big"}}, None, "deepseek")
    assert huge.code == ErrorCode.CONTEXT_LENGTH
    unknown = map_http_error(418, "teapot", None, "deepseek")
    assert unknown.code == ErrorCode.UNKNOWN
    assert unknown.message == "teapot"


def test_map_error_body_and_http_date_retry_after():
    err = map_error_body(
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
        "anthropic",
    )
    assert err.code == ErrorCode.OVERLOADED
    moment = datetime.now(timezone.utc) + timedelta(seconds=30)
    parsed = map_http_error(429, {}, {"Retry-After": format_datetime(moment)}, "anthropic")
    assert parsed.retry_after is not None
    assert 20 <= parsed.retry_after <= 40
