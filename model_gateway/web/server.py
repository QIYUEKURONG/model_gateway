"""本地控制台。只监听 127.0.0.1，用来看一次调用的全过程。"""

from __future__ import annotations

import argparse
import json
import os
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from pydantic import ValidationError

from model_gateway.errors import ErrorCode, GatewayError
from model_gateway.gateway import ModelGateway
from model_gateway.models import ChatRequest, MaterialInput, MaterialSpec, PromptSpec
from model_gateway.sse import decode_sse, encode_sse
from model_gateway.web.envfile import load_env_file, update_env_file

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 1_000_000
_SETTINGS_LOCK = threading.Lock()
_PROVIDER_FIELDS = (
    ("deepseek", "deepseek_api_key", "deepseek_base_url", "clear_deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"),
    ("anthropic", "anthropic_api_key", "anthropic_base_url", "clear_anthropic", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"),
)


def create_server(
    gateway: ModelGateway,
    host: str = "127.0.0.1",
    port: int = 8765,
    env_path: Path | None = None,
) -> ThreadingHTTPServer:
    handler = _handler_factory(gateway, env_path or Path.cwd() / ".env")
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="模型网关网页入口")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--env-file", default="", help="密钥文件，默认是当前目录的 .env")
    args = parser.parse_args()
    env_path = Path(args.env_file).expanduser() if args.env_file else Path.cwd() / ".env"
    load_env_file(env_path)
    gateway = ModelGateway.from_env()
    server = create_server(gateway, args.host, args.port, env_path=env_path)
    url = f"http://{args.host}:{args.port}"
    print(f"模型网关控制台: {url}", flush=True)
    print("密钥从环境变量或 .env 读取，页面上不会显示。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
        gateway.close()


def _handler_factory(gateway: ModelGateway, env_path: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/":
                self._file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
                return
            if path.startswith("/static/"):
                name = path.removeprefix("/static/")
                target = (STATIC_DIR / name).resolve()
                if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
                    self._json(404, {"code": "INVALID_REQUEST", "message": "文件不存在"})
                    return
                kind = "text/css; charset=utf-8" if target.suffix == ".css" else "text/javascript; charset=utf-8"
                self._file(target, kind)
                return
            if path == "/api/status":
                self._json(200, _status(gateway))
                return
            if path == "/api/settings":
                self._json(200, _public_settings(gateway))
                return
            if path == "/api/prompts":
                self._json(200, {"prompts": [_dump(item) for item in gateway.prompts.list_prompts()]})
                return
            if path == "/api/materials":
                self._json(200, {"materials": [_dump(item) for item in gateway.prompts.list_materials()]})
                return
            if path == "/api/usage":
                calls = gateway.usage.list_calls()
                recent = list(reversed(calls[-30:]))
                self._json(
                    200,
                    {
                        "summary": _dump(gateway.usage.summary()),
                        "calls": [_dump(item) for item in recent],
                    },
                )
                return
            self._json(404, {"code": "INVALID_REQUEST", "message": "路径不存在"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                payload = self._read_json()
            except ValueError as exc:
                self._json(400, {"code": "INVALID_REQUEST", "message": str(exc)})
                return
            if not self._same_origin():
                self._json(403, {"code": "PERMISSION_DENIED", "message": "拒绝跨站请求"})
                return
            routes: dict[str, Callable[[dict[str, Any]], None]] = {
                "/api/prompts": self._publish_prompt,
                "/api/materials": self._publish_material,
                "/api/render": self._render,
                "/api/chat": self._chat,
                "/api/complete": self._complete,
                "/api/settings": self._save_settings,
            }
            route = routes.get(path)
            if route is None:
                self._json(404, {"code": "INVALID_REQUEST", "message": "路径不存在"})
                return
            route(payload)

        def _save_settings(self, payload: dict[str, Any]) -> None:
            try:
                saved = apply_settings(gateway, env_path, payload)
            except ValueError as exc:
                self._json(400, {"code": "INVALID_REQUEST", "message": str(exc)})
                return
            self._json(200, saved)

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            host = self.headers.get("Host", "")
            return origin.rstrip("/") in {f"http://{host}", f"https://{host}"}

        def _publish_prompt(self, payload: dict[str, Any]) -> None:
            try:
                saved = gateway.prompts.publish(PromptSpec.model_validate(payload))
            except (ValidationError, GatewayError) as exc:
                self._error(exc)
                return
            self._json(200, _dump(saved))

        def _publish_material(self, payload: dict[str, Any]) -> None:
            try:
                saved = gateway.prompts.put_material(MaterialSpec.model_validate(payload))
            except (ValidationError, GatewayError) as exc:
                self._error(exc)
                return
            self._json(200, _dump(saved))

        def _render(self, payload: dict[str, Any]) -> None:
            try:
                rendered = _render_binding(gateway, payload)
            except (ValidationError, GatewayError) as exc:
                self._error(exc)
                return
            self._json(200, _render_view(rendered))

        def _complete(self, payload: dict[str, Any]) -> None:
            try:
                body = complete_call(gateway, payload)
            except (ValidationError, GatewayError, json.JSONDecodeError) as exc:
                self._error(exc)
                return
            self._json(200, body)

        def _chat(self, payload: dict[str, Any]) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            saw_error = False
            try:
                for frame in _chat_frames(gateway, payload):
                    event, _data = decode_sse(frame)
                    if event == "error":
                        saw_error = True
                    self._chunk(frame)
            except GatewayError as exc:
                if not saw_error:
                    self._chunk(
                        encode_sse("error", {"code": exc.code.value, "message": exc.message})
                    )
            except Exception as exc:
                self._chunk(encode_sse("error", {"code": "UNKNOWN", "message": str(exc)}))
            finally:
                try:
                    self._chunk(encode_sse("stage", {"step": "ledger", "summary": _dump(gateway.usage.summary())}))
                    self._chunk_end()
                except BrokenPipeError:
                    return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_BODY:
                raise ValueError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError("请求体不是 JSON") from exc
            if not isinstance(data, dict):
                raise ValueError("请求体必须是对象")
            return data

        def _file(self, path: Path, content_type: str) -> None:
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, exc: Exception) -> None:
            if isinstance(exc, GatewayError):
                self._json(400, {"code": exc.code.value, "message": exc.message})
                return
            if isinstance(exc, ValidationError):
                first = exc.errors()[0]
                loc = ".".join(str(part) for part in first.get("loc", []))
                message = first.get("msg", "参数不合法")
                if loc:
                    message = f"{loc}: {message}"
                self._json(400, {"code": "INVALID_REQUEST", "message": message})
                return
            self._json(400, {"code": "INVALID_REQUEST", "message": str(exc)})

        def _chunk(self, text: str) -> None:
            data = text.encode("utf-8")
            self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
            self.wfile.write(data)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        def _chunk_end(self) -> None:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[web] {self.address_string()} {fmt % args}")

    return Handler


def complete_call(gateway: ModelGateway, payload: dict[str, Any]) -> dict[str, Any]:
    """等 complete() 整段返回。页面在等待时自己计时，这里不推流。"""
    try:
        request = ChatRequest.model_validate(_chat_payload(payload))
    except json.JSONDecodeError as exc:
        raise GatewayError(ErrorCode.INVALID_REQUEST, "json_schema 不是合法 JSON") from exc
    rendered = _preview_render(gateway, request)
    try:
        response = gateway.complete(request)
    except GatewayError as exc:
        return {
            "ok": False,
            "code": exc.code.value,
            "message": exc.message,
            "content": "",
            "parsed": None,
            "render": rendered,
            "summary": _dump(gateway.usage.summary()),
        }
    return {
        "ok": True,
        "code": "OK",
        "message": "",
        "content": response.content,
        "parsed": response.parsed,
        "model": response.model,
        "provider": response.provider,
        "latency_ms": response.latency_ms,
        "total_tokens": response.usage.total_tokens,
        "cost_usd": format(response.cost.total_cost, "f"),
        "price_tier": response.cost.price_tier,
        "finish_reason": response.finish_reason,
        "render": rendered,
        "summary": _dump(gateway.usage.summary()),
    }


def _preview_render(gateway: ModelGateway, request: ChatRequest) -> dict[str, Any] | None:
    if request.prompt is None:
        return None
    try:
        rendered = gateway.render_prompt(
            request.prompt.key,
            version=request.prompt.version,
            variables=request.prompt.variables,
            materials=request.prompt.materials,
        )
    except GatewayError:
        return None
    return _render_view(rendered)


def _chat_frames(gateway: ModelGateway, payload: dict[str, Any]):
    try:
        request = ChatRequest.model_validate(_chat_payload(payload))
    except json.JSONDecodeError:
        yield encode_sse("error", {"code": "INVALID_REQUEST", "message": "json_schema 不是合法 JSON"})
        return
    except ValidationError as exc:
        yield encode_sse("error", {"code": "INVALID_REQUEST", "message": _validation_message(exc)})
        return
    yield encode_sse(
        "stage",
        {
            "step": "validate",
            "provider": request.provider,
            "model": request.model,
            "response_format": request.response_format.type if request.response_format else "text",
        },
    )
    if request.prompt is not None:
        rendered = gateway.render_prompt(
            request.prompt.key,
            version=request.prompt.version,
            variables=request.prompt.variables,
            materials=request.prompt.materials,
        )
        yield encode_sse("stage", {"step": "render", **_render_view(rendered)})
    else:
        yield encode_sse("stage", {"step": "render", "skipped": True, "text": "", "materials": []})
    yield encode_sse(
        "stage",
        {"step": "call", "provider": request.provider, "model": request.model or "默认模型"},
    )
    yield from gateway.stream_sse(request)


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(part) for part in first.get("loc", []))
    message = first.get("msg", "参数不合法")
    return f"{loc}: {message}" if loc else message


def _chat_payload(payload: dict[str, Any]) -> dict[str, Any]:
    user_message = str(payload.get("user_message") or "").strip()
    messages = [{"role": "user", "content": user_message}] if user_message else []
    body: dict[str, Any] = {
        "provider": payload.get("provider") or "deepseek",
        "messages": messages,
    }
    model = str(payload.get("model") or "").strip()
    if model:
        body["model"] = model
    prompt = payload.get("prompt")
    if prompt:
        body["prompt"] = prompt
    fmt = payload.get("response_format") or "text"
    if fmt == "json_object":
        body["response_format"] = {"type": "json_object"}
    elif fmt == "json_schema":
        schema = payload.get("json_schema")
        if isinstance(schema, str):
            schema = json.loads(schema) if schema.strip() else None
        body["response_format"] = {"type": "json_schema", "json_schema": schema}
    return body


def _render_binding(gateway: ModelGateway, payload: dict[str, Any]):
    prompt = payload.get("prompt") or {}
    materials = [MaterialInput.model_validate(item) for item in prompt.get("materials") or []]
    return gateway.render_prompt(
        prompt["key"],
        version=prompt.get("version"),
        variables=prompt.get("variables") or {},
        materials=materials,
    )


def _render_view(rendered) -> dict[str, Any]:
    return {
        "prompt_key": rendered.prompt_key,
        "version": rendered.version,
        "role": rendered.role,
        "text": rendered.text,
        "variables_used": rendered.variables_used,
        "materials": [_dump(item) for item in rendered.materials],
        "composed_text": rendered.composed_text(),
    }


def _status(gateway: ModelGateway) -> dict[str, bool]:
    def configured(name: str) -> bool:
        provider = gateway.providers.get(name)
        return bool(getattr(provider, "_api_key", ""))

    return {
        "deepseek_configured": configured("deepseek"),
        "anthropic_configured": configured("anthropic"),
    }


def _public_settings(gateway: ModelGateway) -> dict[str, Any]:
    settings: dict[str, Any] = dict(_status(gateway))
    settings["deepseek_base_url"] = _base_url(gateway, "deepseek", "https://api.deepseek.com")
    settings["anthropic_base_url"] = _base_url(gateway, "anthropic", "https://api.anthropic.com")
    return settings


def _base_url(gateway: ModelGateway, name: str, default: str) -> str:
    provider = gateway.providers.get(name)
    value = getattr(provider, "base_url", "") if provider is not None else ""
    return value or default


def apply_settings(gateway: ModelGateway, env_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """把页面提交的密钥应用到当前进程，并写入 .env。返回值里没有密钥。"""
    planned: list[tuple[Any, str, str, bool, str, str]] = []
    updates: dict[str, str] = {}
    for name, key_field, base_field, clear_field, env_key, env_base in _PROVIDER_FIELDS:
        provider = gateway.providers.get(name)
        if provider is None or not hasattr(provider, "_api_key"):
            continue
        clear = bool(payload.get(clear_field))
        key = _clean_secret(payload.get(key_field, ""))
        base = _clean_base_url(payload.get(base_field, ""))
        if clear:
            updates[env_key] = ""
        elif key:
            updates[env_key] = key
        if base:
            updates[env_base] = base
        planned.append((provider, key, base, clear, env_key, env_base))
    with _SETTINGS_LOCK:
        if updates:
            update_env_file(env_path, updates)
        for provider, key, base, clear, env_key, env_base in planned:
            if clear:
                provider._api_key = ""
                os.environ.pop(env_key, None)
            elif key:
                provider._api_key = key
                os.environ[env_key] = key
            if base:
                provider.base_url = base.rstrip("/")
                os.environ[env_base] = base.rstrip("/")
    return _public_settings(gateway)


def _clean_secret(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("密钥必须是字符串")
    secret = value.strip()
    if any(char in secret for char in "\r\n\x00"):
        raise ValueError("密钥里不能有换行")
    if len(secret) > 500:
        raise ValueError("密钥过长")
    return secret


def _clean_base_url(value: Any) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise ValueError("地址必须是字符串")
    url = value.strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("地址需要以 http:// 或 https:// 开头")
    if any(char in url for char in "\r\n\x00 "):
        raise ValueError("地址不合法")
    return url.rstrip("/")


def _dump(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return json.loads(model.model_dump_json())
    if isinstance(model, Decimal):
        return {"value": format(model, "f")}
    raise TypeError(type(model).__name__)
