"""services/unified_media 单元测试：请求构造、文本提取、脱敏日志。"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from memoloupe.core.errors import CapabilityUnavailableError
from memoloupe.services.base import PermanentServiceError, TransientServiceError
from memoloupe.services.unified_media import (
    AnalysisGroup,
    ModelClip,
    OpenAICompatibleUnifiedMedia,
    UnifiedMediaService,
)

API_KEY = "sk-unified-test-key"


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
        if behavior.get("sleep"):
            time.sleep(behavior["sleep"])
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
        try:
            self.wfile.write(resp)
        except (BrokenPipeError, ConnectionResetError):
            pass

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


def _clips(tmp_path: Path) -> list[ModelClip]:
    clips = []
    for i, sid in enumerate(["SH0001", "SH0002"]):
        p = tmp_path / f"{sid}.mp4"
        p.write_bytes(f"video-bytes-{i}".encode())
        clips.append(ModelClip(shot_id=sid, proxy_path=p, duration_ms=1000 + i))
    return clips


def _group(name: str = "visual") -> AnalysisGroup:
    return AnalysisGroup(
        name=name,
        fields=("visual.content", "visual.framing"),
        prompt="分析镜头并输出 JSON",
        schema={"type": "object"},
        fingerprint="fp-" + name,
    )


def _make(server, **kwargs) -> OpenAICompatibleUnifiedMedia:
    return OpenAICompatibleUnifiedMedia(
        base_url=server.url,
        api_key=API_KEY,
        model="ml-pro",
        video_fps=7.5,
        media_resolution="max",
        max_completion_tokens=2048,
        thinking_mode="disabled",
        **kwargs,
    )


def _chat_response(text: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"total_tokens": 10},
    }


class TestUnconfigured:
    def test_empty_api_key_raises(self):
        with pytest.raises(CapabilityUnavailableError) as exc_info:
            OpenAICompatibleUnifiedMedia(base_url="http://x", api_key="", model="m")
        assert exc_info.value.capability == "unifiedModel"

    def test_fallback_model_stored(self, server):
        svc = _make(server, fallback_model="ml-lite")
        assert svc.fallback_model == "ml-lite"


class TestAnalyzeBatch:
    def test_extracts_message_content_verbatim(self, server, tmp_path):
        text = '{"shots": [{"shotID": "SH0001"}]}'
        server.handler.behavior = {"body": _chat_response(text)}
        result = _make(server).analyze_batch(_clips(tmp_path), _group())
        assert result == text

    def test_structured_field_preferred_over_content(self, server, tmp_path):
        server.handler.behavior = {
            "body": {
                "choices": [
                    {
                        "message": {
                            "parsed": {"shots": [{"shotID": "SH0001"}]},
                            "content": "fallback text",
                        }
                    }
                ]
            }
        }
        result = _make(server).analyze_batch(_clips(tmp_path), _group())
        assert json.loads(result) == {"shots": [{"shotID": "SH0001"}]}

    def test_fence_text_returned_verbatim(self, server, tmp_path):
        # fence 剥离是编排器职责；端口原样返回
        fenced = '```json\n{"shots": []}\n```'
        server.handler.behavior = {"body": _chat_response(fenced)}
        assert _make(server).analyze_batch(_clips(tmp_path), _group()) == fenced

    def test_content_parts_list_joined(self, server, tmp_path):
        server.handler.behavior = {
            "body": {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": '{"a":'},
                                {"type": "text", "text": "1}"},
                            ]
                        }
                    }
                ]
            }
        }
        assert _make(server).analyze_batch(_clips(tmp_path), _group()) == '{"a":1}'

    def test_unextractable_response_is_permanent(self, server, tmp_path):
        server.handler.behavior = {"body": {"choices": []}}
        with pytest.raises(PermanentServiceError):
            _make(server).analyze_batch(_clips(tmp_path), _group())

    def test_request_payload_shape(self, server, tmp_path):
        clips = _clips(tmp_path)
        server.handler.behavior = {"body": _chat_response("{}")}
        _make(server).analyze_batch(clips, _group())
        captured = server.handler.captured
        assert captured["path"] == "/chat/completions"
        assert captured["headers"].get("Authorization") == f"Bearer {API_KEY}"
        payload = json.loads(captured["body"])
        assert payload["model"] == "ml-pro"
        assert 0.0 <= payload["temperature"] <= 0.2
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["max_completion_tokens"] == 2048
        assert payload["thinking"] == {"type": "disabled"}
        content = payload["messages"][0]["content"]
        request_text = content[-1]
        assert request_text["type"] == "text"
        assert request_text["text"].startswith("分析镜头并输出 JSON")
        assert "第 1 个 video_url（视频） = SH0001" in request_text["text"]
        assert "第 2 个 video_url（视频） = SH0002" in request_text["text"]
        video_parts = [p for p in content if p["type"] == "video_url"]
        assert len(video_parts) == 2
        for clip, part in zip(clips, video_parts):
            assert part["fps"] == 7.5
            assert part["media_resolution"] == "max"
            url = part["video_url"]["url"]
            assert url.startswith("data:video/mp4;base64,")
            assert base64.b64decode(url.split(",", 1)[1]) == clip.proxy_path.read_bytes()

    def test_image_clip_uses_image_url_part(self, server, tmp_path):
        img = tmp_path / "SH0003.jpg"
        img.write_bytes(b"jpeg-bytes")
        clips = _clips(tmp_path) + [
            ModelClip(shot_id="SH0003", proxy_path=img, duration_ms=600)
        ]
        server.handler.behavior = {"body": _chat_response("{}")}
        _make(server).analyze_batch(clips, _group())
        payload = json.loads(server.handler.captured["body"])
        content = payload["messages"][0]["content"]
        image_parts = [p for p in content if p["type"] == "image_url"]
        assert len(image_parts) == 1
        part = image_parts[0]
        assert "fps" not in part and "media_resolution" not in part
        url = part["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        assert base64.b64decode(url.split(",", 1)[1]) == b"jpeg-bytes"
        request_text = content[-1]["text"]
        assert "第 3 个 image_url（静态图像） = SH0003" in request_text

    def test_is_image_property(self, tmp_path):
        assert ModelClip(
            shot_id="SH0001", proxy_path=tmp_path / "a.JPG", duration_ms=1
        ).is_image
        assert not ModelClip(
            shot_id="SH0001", proxy_path=tmp_path / "a.mp4", duration_ms=1
        ).is_image

    @pytest.mark.parametrize("status", [429, 500, 503])
    def test_transient_status(self, server, tmp_path, status):
        server.handler.behavior = {"status": status, "body": {"e": 1}}
        with pytest.raises(TransientServiceError):
            _make(server).analyze_batch(_clips(tmp_path), _group())

    @pytest.mark.parametrize("status", [400, 401, 403])
    def test_permanent_status(self, server, tmp_path, status):
        server.handler.behavior = {"status": status, "body": {"e": 1}}
        with pytest.raises(PermanentServiceError):
            _make(server).analyze_batch(_clips(tmp_path), _group())

    def test_timeout_is_transient(self, server, tmp_path):
        server.handler.behavior = {"sleep": 1.5, "body": _chat_response("{}")}
        svc = _make(server, timeout_sec=0.2)
        with pytest.raises(TransientServiceError):
            svc.analyze_batch(_clips(tmp_path), _group())

    def test_missing_clip_file_is_permanent(self, server, tmp_path):
        clip = ModelClip(
            shot_id="SH0009", proxy_path=tmp_path / "gone.mp4", duration_ms=100
        )
        with pytest.raises(PermanentServiceError):
            _make(server).analyze_batch([clip], _group())

    def test_satisfies_protocol(self, server):
        assert isinstance(_make(server), UnifiedMediaService)


class TestLogRedaction:
    def test_debug_log_has_metadata_but_no_secrets(self, server, tmp_path, caplog):
        server.handler.behavior = {"body": _chat_response("{}")}
        with caplog.at_level(logging.DEBUG):
            _make(server).analyze_batch(_clips(tmp_path), _group())
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "ml-pro" in text
        assert "SH0001" in text
        assert API_KEY not in text
        assert "data:video/mp4" not in text
        assert "video-bytes" not in text

    def test_error_log_redacted(self, server, tmp_path, caplog):
        server.handler.behavior = {"status": 401, "body": {"key": API_KEY}}
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(PermanentServiceError):
                _make(server).analyze_batch(_clips(tmp_path), _group())
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert API_KEY not in text
        assert "data:video/mp4" not in text
