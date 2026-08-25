"""services/base 单元测试：HTTP JSON POST、错误分类与脱敏。"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from memoloupe.services.base import (
    PermanentServiceError,
    ServiceError,
    TransientServiceError,
    http_json_post,
    redact_text,
)


class _Handler(BaseHTTPRequestHandler):
    """由测试类属性驱动的可编程 HTTP 服务。"""

    behavior: dict = {}
    captured: dict = {}

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        type(self).captured = {
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        }
        behavior = type(self).behavior
        if behavior.get("sleep"):
            time.sleep(behavior["sleep"])
        status = behavior.get("status", 200)
        resp = behavior.get("body", b"{}")
        if callable(resp):
            resp = resp(self)
        if isinstance(resp, (dict, list)):
            resp = json.dumps(resp).encode("utf-8")
        elif isinstance(resp, str):
            resp = resp.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        try:
            self.wfile.write(resp)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):  # 静默测试服务
        pass


@pytest.fixture
def server():
    _Handler.behavior = {}
    _Handler.captured = {}
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(
        url=f"http://127.0.0.1:{httpd.server_port}",
        handler=_Handler,
    )
    httpd.shutdown()
    thread.join(timeout=5)


class TestRedactText:
    def test_replaces_all_occurrences(self):
        assert redact_text("key=sk-abc see sk-abc", ["sk-abc"]) == "key=*** see ***"

    def test_ignores_empty_secrets(self):
        assert redact_text("hello", ["", None]) == "hello"  # type: ignore[list-item]

    def test_multiple_secrets(self):
        assert redact_text("a1b2", ["a1", "b2"]) == "******"


class TestHttpJsonPost:
    def test_success_returns_parsed_dict(self, server):
        server.handler.behavior = {"body": {"ok": True, "n": 1}}
        result = http_json_post(
            server.url + "/v1/test",
            headers={"Authorization": "Bearer sk-secret"},
            payload={"model": "m"},
            timeout_sec=5,
        )
        assert result == {"ok": True, "n": 1}
        captured = server.handler.captured
        assert captured["path"] == "/v1/test"
        assert json.loads(captured["body"]) == {"model": "m"}
        assert captured["headers"].get("Authorization") == "Bearer sk-secret"

    @pytest.mark.parametrize("status", [429, 500, 502, 503])
    def test_transient_status(self, server, status):
        server.handler.behavior = {"status": status, "body": {"err": "x"}}
        with pytest.raises(TransientServiceError):
            http_json_post(server.url, headers={}, payload={}, timeout_sec=5)

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_permanent_status(self, server, status):
        server.handler.behavior = {"status": status, "body": {"err": "x"}}
        with pytest.raises(PermanentServiceError):
            http_json_post(server.url, headers={}, payload={}, timeout_sec=5)

    def test_non_json_response_is_permanent(self, server):
        server.handler.behavior = {"body": "not json at all"}
        with pytest.raises(PermanentServiceError):
            http_json_post(server.url, headers={}, payload={}, timeout_sec=5)

    def test_json_array_response_is_permanent(self, server):
        server.handler.behavior = {"body": [1, 2, 3]}
        with pytest.raises(PermanentServiceError):
            http_json_post(server.url, headers={}, payload={}, timeout_sec=5)

    def test_connection_refused_is_transient(self):
        # 绑定后立即关闭，拿到一个几乎肯定拒绝连接的端口。
        httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        port = httpd.server_port
        httpd.server_close()
        with pytest.raises(TransientServiceError):
            http_json_post(
                f"http://127.0.0.1:{port}", headers={}, payload={}, timeout_sec=2
            )

    def test_timeout_is_transient(self, server):
        server.handler.behavior = {"sleep": 1.5, "body": {}}
        with pytest.raises(TransientServiceError):
            http_json_post(server.url, headers={}, payload={}, timeout_sec=0.2)

    def test_error_message_redacts_authorization(self, server):
        key = "sk-super-secret-key"

        def echo_auth(handler):
            return json.dumps({"echo": handler.headers.get("Authorization")})

        server.handler.behavior = {"status": 400, "body": echo_auth}
        with pytest.raises(PermanentServiceError) as exc_info:
            http_json_post(
                server.url,
                headers={"Authorization": f"Bearer {key}"},
                payload={"model": "m"},
                timeout_sec=5,
            )
        text = str(exc_info.value)
        assert key not in text
        assert "Authorization" not in text

    def test_error_message_never_contains_payload(self, server):
        server.handler.behavior = {"status": 500, "body": {"err": "boom"}}
        with pytest.raises(TransientServiceError) as exc_info:
            http_json_post(
                server.url,
                headers={},
                payload={"prompt": "绝密提示词内容"},
                timeout_sec=5,
            )
        assert "绝密提示词内容" not in str(exc_info.value)

    def test_service_error_hierarchy(self):
        assert issubclass(TransientServiceError, ServiceError)
        assert issubclass(PermanentServiceError, ServiceError)
