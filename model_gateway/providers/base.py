"""供应商共用的 HTTP 调用。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import httpx

from model_gateway.errors import ErrorCode, GatewayError, map_http_error
from model_gateway.models import TokenUsage


@dataclass
class ProviderResult:
    id: str
    model: str
    content: str
    finish_reason: str | None
    usage: TokenUsage


@dataclass
class ProviderEvent:
    text: str = ""
    usage: TokenUsage | None = None
    finish_reason: str | None = None
    response_id: str | None = None
    model: str | None = None


class BaseProvider:
    """两家供应商共用的 HTTP 层。密钥放在 _api_key，repr 里固定显示成 ***。"""
    name = ""
    default_model = ""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r}, base_url={self.base_url!r}, api_key='***')"

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _require_key(self) -> None:
        if not self._api_key:
            raise GatewayError(
                ErrorCode.AUTHENTICATION,
                f"未配置 {self.name} API key",
                provider=self.name,
            )

    def _post(self, url: str, payload: dict, headers: dict[str, str]) -> httpx.Response:
        try:
            response = self.client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise _transport(exc, self.name) from exc
        except httpx.TransportError as exc:
            raise _transport(exc, self.name) from exc
        if response.status_code >= 400:
            raise self._http_error(response)
        return response

    def _iter_lines(self, url: str, payload: dict, headers: dict[str, str]) -> Iterator[str]:
        try:
            with self.client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code >= 400:
                    raise self._http_error(response)
                yield from response.iter_lines()
        except GatewayError:
            raise
        except httpx.TimeoutException as exc:
            raise _transport(exc, self.name) from exc
        except httpx.TransportError as exc:
            raise _transport(exc, self.name) from exc

    def _http_error(self, response: httpx.Response) -> GatewayError:
        raw = response.read()
        text = raw.decode("utf-8", errors="replace")
        try:
            import json

            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            payload = {"message": text}
        return map_http_error(response.status_code, payload, response.headers, self.name)


def _transport(exc: Exception, provider: str) -> GatewayError:
    if isinstance(exc, httpx.TimeoutException):
        code = ErrorCode.TIMEOUT
    else:
        code = ErrorCode.NETWORK_ERROR
    return GatewayError(code, str(exc) or exc.__class__.__name__, provider=provider)
