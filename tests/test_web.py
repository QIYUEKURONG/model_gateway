import json
import os
import threading
from http.client import HTTPConnection

from model_gateway.gateway import ModelGateway
from model_gateway.models import TokenUsage
from model_gateway.providers.base import ProviderResult
from model_gateway.web.envfile import load_env_file
from model_gateway.web.server import create_server


def test_web_shows_render_process_and_records_missing_key(tmp_path):
    gateway = ModelGateway(db_path=tmp_path / "web.db", deepseek_api_key="")
    server = create_server(gateway, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        page = _request(port, "GET", "/")
        assert page.status == 200
        assert "模型网关" in page.text

        status = _request(port, "GET", "/api/status").json()
        assert "deepseek_api_key" not in json.dumps(status)
        assert status["deepseek_configured"] is False

        published = _request(
            port,
            "POST",
            "/api/prompts",
            {
                "key": "trip.plan",
                "body": "Plan {city}.",
                "slots": [
                    {"name": "city", "kind": "variable"},
                    {"name": "policy", "kind": "external"},
                ],
            },
        ).json()
        assert published["version"] == 1

        rendered = _request(
            port,
            "POST",
            "/api/render",
            {
                "prompt": {
                    "key": "trip.plan",
                    "variables": {"city": "Paris"},
                    "materials": [{"slot": "policy", "content": "原文 {city}"}],
                }
            },
        ).json()
        assert rendered["text"] == "Plan Paris."
        assert rendered["materials"][0]["content"] == "原文 {city}"
        assert "原文 {city}" not in rendered["text"]

        frames = _read_sse(
            port,
            "/api/chat",
            {
                "provider": "deepseek",
                "user_message": "go",
                "prompt": {
                    "key": "trip.plan",
                    "variables": {"city": "Paris"},
                    "materials": [{"slot": "policy", "content": "原文 {city}"}],
                },
            },
        )
        events = [item[0] for item in frames]
        assert "validate" in [item[1].get("step") for item in frames if item[0] == "stage"]
        assert any(item[0] == "error" and item[1]["code"] == "AUTHENTICATION" for item in frames)
        assert events[-1] == "stage" and frames[-1][1]["step"] == "ledger"
        usage = _request(port, "GET", "/api/usage").json()
        assert usage["summary"]["errors"] == 1
        assert "sk-" not in json.dumps(usage)
    finally:
        server.shutdown()
        gateway.close()


def test_web_settings_saves_key_without_echoing_it(tmp_path):
    secret = "sk-local-demo"
    env_path = tmp_path / ".env"
    env_path.write_text("OTHER=keep\n# note\n", encoding="utf-8")
    names = ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL")
    previous = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ.pop(name, None)
    gateway = ModelGateway(db_path=tmp_path / "web.db", deepseek_api_key="")
    server = create_server(gateway, "127.0.0.1", 0, env_path=env_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        page = _request(port, "GET", "/")
        assert "配置" in page.text

        blocked = _request(
            port,
            "POST",
            "/api/settings",
            {"deepseek_api_key": secret},
            {"Origin": "https://evil.example"},
        )
        assert blocked.status == 403
        assert secret not in env_path.read_text(encoding="utf-8")

        saved = _request(
            port,
            "POST",
            "/api/settings",
            {"deepseek_api_key": secret, "deepseek_base_url": "https://example.invalid/v1"},
        )
        assert saved.status == 200
        body = json.dumps(saved.json())
        assert saved.json()["deepseek_configured"] is True
        assert secret not in body
        assert "deepseek_api_key" not in body

        status = _request(port, "GET", "/api/status").json()
        settings = _request(port, "GET", "/api/settings").json()
        assert status["deepseek_configured"] is True
        assert settings["deepseek_base_url"] == "https://example.invalid/v1"
        assert secret not in json.dumps(status)
        assert secret not in json.dumps(settings)

        stored = env_path.read_text(encoding="utf-8")
        assert "OTHER=keep" in stored
        assert "# note" in stored
        assert f"DEEPSEEK_API_KEY={secret}" in stored
        assert env_path.stat().st_mode & 0o777 == 0o600
        assert secret.encode() not in (tmp_path / "web.db").read_bytes()
        assert gateway.providers["deepseek"]._api_key == secret

        kept = _request(port, "POST", "/api/settings", {"deepseek_api_key": ""}).json()
        assert kept["deepseek_configured"] is True
        cleared = _request(port, "POST", "/api/settings", {"clear_deepseek": True}).json()
        assert cleared["deepseek_configured"] is False
        assert "DEEPSEEK_API_KEY" not in env_path.read_text(encoding="utf-8")
        assert gateway.providers["deepseek"]._api_key == ""

        rejected = _request(port, "POST", "/api/settings", {"deepseek_base_url": "not-a-url"})
        assert rejected.status == 400
        assert secret not in rejected.text
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        server.shutdown()
        gateway.close()


def test_web_complete_returns_the_whole_reply_at_once(tmp_path):
    class _Once:
        def __init__(self):
            self.name = "deepseek"
            self.default_model = "deepseek-v4-flash"
            self.calls = 0

        def complete(self, request):
            self.calls += 1
            return ProviderResult(
                id="chatcmpl-web",
                model="deepseek-v4-flash",
                content='{"city":"Paris"}',
                finish_reason="stop",
                usage=TokenUsage(input_tokens=10, output_tokens=4, uncached_input_tokens=10, total_tokens=14),
            )

        def stream(self, request):
            raise AssertionError("非流式不应调用 stream")

        def close(self):
            return None

    provider = _Once()
    gateway = ModelGateway(db_path=tmp_path / "web.db", providers={"deepseek": provider})
    server = create_server(gateway, "127.0.0.1", 0, env_path=tmp_path / ".env")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        page = _request(port, "GET", "/")
        assert "完成后一次返回" in page.text
        assert 'id="countdown"' in page.text
        saved = _request(
            port,
            "POST",
            "/api/complete",
            {"provider": "deepseek", "user_message": "go", "response_format": "json_object"},
        )
        assert saved.status == 200
        assert "text/event-stream" not in saved.headers.get("content-type", "")
        body = saved.json()
        assert body["ok"] is True
        assert body["content"] == '{"city":"Paris"}'
        assert body["parsed"] == {"city": "Paris"}
        assert provider.calls == 1
        assert "sk-" not in saved.text
        usage = _request(port, "GET", "/api/usage").json()
        assert usage["calls"][0]["stream"] is False
    finally:
        server.shutdown()
        gateway.close()


def test_load_env_file_keeps_existing_process_value(tmp_path):
    path = tmp_path / ".env"
    path.write_text('DEEPSEEK_API_KEY="quoted-value"\n', encoding="utf-8")
    previous = os.environ.get("DEEPSEEK_API_KEY")
    os.environ["DEEPSEEK_API_KEY"] = "already-set"
    try:
        load_env_file(path)
        assert os.environ["DEEPSEEK_API_KEY"] == "already-set"
        os.environ.pop("DEEPSEEK_API_KEY", None)
        load_env_file(path)
        assert os.environ["DEEPSEEK_API_KEY"] == "quoted-value"
    finally:
        if previous is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = previous


class _Response:
    def __init__(self, status, body, headers):
        self.status = status
        self.body = body
        self.headers = {key.lower(): value for key, value in headers}
        self.text = body.decode()

    def json(self):
        return json.loads(self.text)


def _request(port, method, path, payload=None, headers=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    body = json.dumps(payload).encode() if payload is not None else None
    sent = {"Content-Type": "application/json"} if body else {}
    if headers:
        sent.update(headers)
    connection.request(method, path, body=body, headers=sent)
    response = connection.getresponse()
    data = response.read()
    status = response.status
    headers = response.getheaders()
    connection.close()
    return _Response(status, data, headers)


def _read_sse(port, path, payload):
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    raw = json.dumps(payload).encode()
    connection.request("POST", path, body=raw, headers={"Content-Type": "application/json"})
    response = connection.getresponse()
    text = response.read().decode()
    connection.close()
    frames = []
    for frame in text.split("\n\n"):
        if not frame.strip():
            continue
        event = "message"
        data = "{}"
        for line in frame.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = line.split(":", 1)[1].strip()
        frames.append((event, json.loads(data)))
    return frames
