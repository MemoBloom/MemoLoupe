"""services/asr 单元测试：归一化、脱敏与未配置降级。"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from memoloupe.core.errors import CapabilityUnavailableError
from memoloupe.services.asr import (
    ASRRequest,
    ASRResult,
    ASRService,
    MultipartOpenAICompatibleASR,
    OpenAICompatibleASR,
    PROVIDER_JSON,
    PROVIDER_MULTIPART,
    _multipart_body,
    build_asr_service,
)
from memoloupe.services.base import PermanentServiceError, TransientServiceError

API_KEY = "sk-asr-test-key"


class _Handler(BaseHTTPRequestHandler):
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
        status = behavior.get("status", 200)
        resp = behavior.get("body", b"{}")
        if isinstance(resp, (dict, list)):
            resp = json.dumps(resp).encode("utf-8")
        elif isinstance(resp, str):
            resp = resp.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    _Handler.behavior = {}
    _Handler.captured = {}
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{httpd.server_port}", handler=_Handler)
    httpd.shutdown()
    thread.join(timeout=5)


@pytest.fixture
def media_file(tmp_path: Path) -> Path:
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\x00\x01fake-media-bytes")
    return p


def _make(server, **kwargs) -> OpenAICompatibleASR:
    return OpenAICompatibleASR(
        base_url=server.url, api_key=API_KEY, model="whisper-x", **kwargs
    )


class TestUnconfigured:
    def test_empty_api_key_raises_capability_unavailable(self):
        with pytest.raises(CapabilityUnavailableError) as exc_info:
            OpenAICompatibleASR(base_url="http://x", api_key="", model="m")
        assert exc_info.value.capability == "asr"

    def test_none_api_key_raises(self):
        with pytest.raises(CapabilityUnavailableError):
            OpenAICompatibleASR(base_url="http://x", api_key=None, model="m")


class TestTranscribe:
    def test_normalizes_verbose_json_segments(self, server, media_file):
        server.handler.behavior = {
            "body": {
                "language": "zh",
                "duration": 3.0,
                "segments": [
                    {
                        "start": 0.5,
                        "end": 1.25,
                        "text": "你好",
                        "speaker": "SPEAKER_00",
                        "confidence": 0.92,
                    },
                    {"start": 2.0, "end": 3.0, "text": "world"},
                ],
                "x_provider_trace": "abc",
            }
        }
        result = _make(server).transcribe(media_file, ASRRequest())
        assert isinstance(result, ASRResult)
        assert len(result.segments) == 2
        first, second = result.segments
        assert first == {
            "startMs": 500,
            "endMs": 1250,
            "text": "你好",
            "speaker": "SPEAKER_00",
            "confidence": 0.92,
        }
        # 缺 speaker → null；缺 confidence → null
        assert second["startMs"] == 2000
        assert second["endMs"] == 3000
        assert second["speaker"] is None
        assert second["confidence"] is None
        # 供应商扩展进入命名空间，不泄漏进 segments
        assert result.raw_extras["provider"]["language"] == "zh"
        assert result.raw_extras["provider"]["x_provider_trace"] == "abc"
        assert "segments" not in result.raw_extras["provider"]

    def test_seconds_to_ms_half_up(self, server, media_file):
        server.handler.behavior = {
            "body": {"segments": [{"start": "0.0005", "end": 1.0005, "text": "x"}]}
        }
        result = _make(server).transcribe(media_file, ASRRequest())
        assert result.segments[0]["startMs"] == 1  # ROUND_HALF_UP
        assert result.segments[0]["endMs"] == 1001  # 1000.5 → 1001

    def test_window_filters_segments_by_overlap(self, server, media_file):
        server.handler.behavior = {
            "body": {
                "segments": [
                    {"start": 0.0, "end": 1.0, "text": "a"},
                    {"start": 1.0, "end": 2.0, "text": "b"},
                    {"start": 5.0, "end": 6.0, "text": "c"},
                ]
            }
        }
        result = _make(server).transcribe(
            media_file, ASRRequest(start_ms=500, end_ms=1500)
        )
        assert [s["text"] for s in result.segments] == ["a", "b"]

    def test_request_format_is_json_with_base64(self, server, media_file):
        server.handler.behavior = {"body": {"segments": []}}
        _make(server).transcribe(media_file, ASRRequest(language="zh"))
        captured = server.handler.captured
        assert captured["path"] == "/audio/transcriptions"
        payload = json.loads(captured["body"])
        assert payload["model"] == "whisper-x"
        assert payload["language"] == "zh"
        assert base64.b64decode(payload["audio_base64"]) == media_file.read_bytes()
        assert captured["headers"].get("Authorization") == f"Bearer {API_KEY}"

    def test_missing_segment_fields_is_permanent(self, server, media_file):
        server.handler.behavior = {"body": {"segments": [{"text": "无时间"}]}}
        with pytest.raises(PermanentServiceError):
            _make(server).transcribe(media_file, ASRRequest())

    def test_segments_not_a_list_is_permanent(self, server, media_file):
        server.handler.behavior = {"body": {"segments": "oops"}}
        with pytest.raises(PermanentServiceError):
            _make(server).transcribe(media_file, ASRRequest())

    def test_401_is_permanent_and_redacted(self, server, media_file):
        server.handler.behavior = {"status": 401, "body": {"error": API_KEY}}
        with pytest.raises(PermanentServiceError) as exc_info:
            _make(server).transcribe(media_file, ASRRequest())
        assert API_KEY not in str(exc_info.value)

    def test_429_is_transient(self, server, media_file):
        server.handler.behavior = {"status": 429, "body": {"error": "slow down"}}
        with pytest.raises(TransientServiceError):
            _make(server).transcribe(media_file, ASRRequest())

    def test_missing_media_file_is_permanent(self, server, tmp_path):
        with pytest.raises(PermanentServiceError):
            _make(server).transcribe(tmp_path / "nope.mp4", ASRRequest())


class TestMultipartAdapter:
    """05-01C：multipart/form-data 上传适配器。"""

    def _make(self, server, **kwargs) -> MultipartOpenAICompatibleASR:
        return MultipartOpenAICompatibleASR(
            base_url=server.url, api_key=API_KEY, model="whisper-x", **kwargs
        )

    def test_multipart_body_contains_fields_and_file(self):
        body, boundary = _multipart_body(
            {"model": "whisper-x", "response_format": "verbose_json"},
            file_field="file",
            filename="clip.mp4",
            file_bytes=b"\x00\x01media",
            content_type="application/octet-stream",
        )
        text = body.decode("utf-8", errors="replace")
        assert boundary in text
        assert 'name="model"' in text and 'whisper-x' in text
        assert 'name="file"; filename="clip.mp4"' in text
        assert b"\x00\x01media" in body
        assert text.endswith(f"--{boundary}--\r\n")

    def test_transcribe_uploads_multipart(self, server, media_file):
        server.handler.behavior = {
            "body": {"segments": [{"start": 0.0, "end": 1.0, "text": "ok"}]}
        }
        result = self._make(server, file_field="audio").transcribe(
            media_file, ASRRequest(language="zh")
        )
        assert result.segments[0]["text"] == "ok"
        captured = server.handler.captured
        content_type = captured["headers"].get("Content-Type", "")
        assert content_type.startswith("multipart/form-data; boundary=")
        boundary = content_type.split("boundary=", 1)[1]
        assert f"--{boundary}" in captured["body"].decode("utf-8", errors="replace")
        assert 'name="audio"; filename="clip.mp4"' in captured["body"].decode(
            "utf-8", errors="replace"
        )
        assert captured["headers"].get("Authorization") == f"Bearer {API_KEY}"

    def test_missing_api_key_raises(self):
        with pytest.raises(CapabilityUnavailableError):
            MultipartOpenAICompatibleASR(base_url="http://x", api_key=None, model="m")


class TestBuildService:
    """05-01C：asr.provider 配置选择适配器。"""

    def _config(self, **overrides) -> dict:
        cfg = {
            "enabled": True,
            "provider": PROVIDER_JSON,
            "baseUrl": "http://asr.example.com",
            "apiKey": "sk-key",
            "model": "whisper-x",
            "fileField": "file",
            "timeoutSec": 30.0,
        }
        cfg.update(overrides)
        return {"asr": cfg}

    def test_default_provider_is_json(self):
        service = build_asr_service(self._config())
        assert isinstance(service, OpenAICompatibleASR)

    def test_multipart_provider(self):
        service = build_asr_service(self._config(provider=PROVIDER_MULTIPART))
        assert isinstance(service, MultipartOpenAICompatibleASR)

    def test_custom_file_field(self):
        service = build_asr_service(
            self._config(provider=PROVIDER_MULTIPART, fileField="audio_file")
        )
        assert service._file_field == "audio_file"

    def test_disabled_returns_none(self):
        assert build_asr_service(self._config(enabled=False)) is None

    def test_missing_credentials_returns_none(self):
        assert build_asr_service(self._config(apiKey=None)) is None
        assert build_asr_service({"asr": {}}) is None

    def test_unknown_provider_falls_back_to_json(self):
        service = build_asr_service(self._config(provider="bogus"))
        assert isinstance(service, OpenAICompatibleASR)

    def test_satisfies_protocol(self, server):
        assert isinstance(_make(server), ASRService)
