"""统一错误码。业务代码只依赖这里的枚举，不直接散落供应商原文。"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any, Mapping


class ErrorCode(str, Enum):
    OK = "OK"
    INVALID_REQUEST = "INVALID_REQUEST"
    AUTHENTICATION = "AUTHENTICATION"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    TIMEOUT = "TIMEOUT"
    OVERLOADED = "OVERLOADED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    PARSE_ERROR = "PARSE_ERROR"
    CONTENT_FILTER = "CONTENT_FILTER"
    CONTEXT_LENGTH = "CONTEXT_LENGTH"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"

    @property
    def retryable(self) -> bool:
        return self in _RETRYABLE


_RETRYABLE = {
    ErrorCode.RATE_LIMITED,
    ErrorCode.TIMEOUT,
    ErrorCode.OVERLOADED,
    ErrorCode.NETWORK_ERROR,
    ErrorCode.PROVIDER_ERROR,
}


class GatewayError(Exception):
    """供应商、网络、校验失败都收成这一种异常。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        provider: str | None = None,
        status_code: int | None = None,
        retry_after: float | None = None,
        usage: Any = None,
        provider_request_id: str | None = None,
        finish_reason: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.provider = provider
        self.status_code = status_code
        self.retry_after = retry_after
        self.usage = usage
        self.provider_request_id = provider_request_id
        self.finish_reason = finish_reason
        self.retryable = code.retryable
        super().__init__(f"[{code.value}] {message}")


class PromptError(GatewayError):
    """提示词缺失、占位符对不上。错误码是 INVALID_REQUEST，不重试。"""
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.INVALID_REQUEST, message)


class SchemaError(GatewayError):
    """返回的 JSON 不符合 schema。错误码是 PARSE_ERROR，不重试。"""
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.PARSE_ERROR, message)


def is_retryable(code: ErrorCode) -> bool:
    return code.retryable


_STATUS = {
    400: ErrorCode.INVALID_REQUEST,
    401: ErrorCode.AUTHENTICATION,
    403: ErrorCode.PERMISSION_DENIED,
    402: ErrorCode.QUOTA_EXCEEDED,
    404: ErrorCode.INVALID_REQUEST,
    408: ErrorCode.TIMEOUT,
    409: ErrorCode.PROVIDER_ERROR,
    413: ErrorCode.CONTEXT_LENGTH,
    422: ErrorCode.INVALID_REQUEST,
    429: ErrorCode.RATE_LIMITED,
    500: ErrorCode.PROVIDER_ERROR,
    502: ErrorCode.PROVIDER_ERROR,
    503: ErrorCode.PROVIDER_ERROR,
    504: ErrorCode.TIMEOUT,
    529: ErrorCode.OVERLOADED,
}

_BODY = {
    "invalid_request_error": ErrorCode.INVALID_REQUEST,
    "authentication_error": ErrorCode.AUTHENTICATION,
    "permission_error": ErrorCode.PERMISSION_DENIED,
    "permission_denied": ErrorCode.PERMISSION_DENIED,
    "not_found_error": ErrorCode.INVALID_REQUEST,
    "request_too_large": ErrorCode.CONTEXT_LENGTH,
    "rate_limit_error": ErrorCode.RATE_LIMITED,
    "rate_limit_exceeded": ErrorCode.RATE_LIMITED,
    "billing_error": ErrorCode.QUOTA_EXCEEDED,
    "insufficient_quota": ErrorCode.QUOTA_EXCEEDED,
    "timeout_error": ErrorCode.TIMEOUT,
    "api_error": ErrorCode.PROVIDER_ERROR,
    "overloaded_error": ErrorCode.OVERLOADED,
    "content_filter": ErrorCode.CONTENT_FILTER,
    "content_filter_error": ErrorCode.CONTENT_FILTER,
}


def map_http_error(
    status_code: int,
    payload: Any,
    headers: Mapping[str, str] | None,
    provider: str,
) -> GatewayError:
    message, body_code = _extract_message(payload, f"HTTP {status_code}")
    code = _BODY.get(body_code) or _STATUS.get(status_code) or (
        ErrorCode.PROVIDER_ERROR if status_code >= 500 else ErrorCode.UNKNOWN
    )
    # 这几个状态码以 HTTP 为准，不用响应体里的 type 盖掉。
    if status_code in (401, 403, 429, 529):
        code = _STATUS[status_code]
    return GatewayError(
        code,
        message,
        provider=provider,
        status_code=status_code,
        retry_after=_retry_after(headers),
    )


def map_error_body(payload: Any, provider: str) -> GatewayError:
    message, body_code = _extract_message(payload, "供应商返回错误")
    code = _BODY.get(body_code, ErrorCode.PROVIDER_ERROR)
    return GatewayError(code, message, provider=provider)


def _extract_message(payload: Any, fallback: str) -> tuple[str, str]:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or fallback)
            marker = str(err.get("type") or err.get("code") or "")
            return message, marker
        if isinstance(err, str):
            return err, str(payload.get("type") or payload.get("code") or "")
        if payload.get("message"):
            return str(payload["message"]), str(payload.get("type") or payload.get("code") or "")
    if isinstance(payload, str) and payload:
        return payload, ""
    return fallback, ""


def _retry_after(headers: Mapping[str, str] | None) -> float | None:
    if not headers:
        return None
    raw = None
    for key, value in headers.items():
        if key.lower() == "retry-after":
            raw = value
            break
    if raw is None or raw == "":
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        moment = parsedate_to_datetime(str(raw))
    except (TypeError, ValueError, IndexError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())

