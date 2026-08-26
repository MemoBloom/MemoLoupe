"""CLI textModel 配置构造测试（Phase 05-01）。"""

from __future__ import annotations

from memoloupe.cli.text_model_config import build_text_model_service
from memoloupe.services.text_model import TextModelRequest


class TestBuildTextModelService:
    def test_unconfigured_returns_warning(self):
        service, warning = build_text_model_service({"textModel": {}})
        assert service is None
        assert warning is not None
        assert "未配置" in warning

    def test_partial_config_returns_warning_without_service(self):
        service, warning = build_text_model_service(
            {"textModel": {"baseUrl": "https://api.example.test", "model": "m"}}
        )
        assert service is None
        assert warning is not None
        assert "apiKey" in warning

    def test_complete_config_builds_service_with_default_max_tokens(self, monkeypatch):
        captured = {}

        def fake_post(url, *, headers, payload, timeout_sec):
            captured.update(
                {
                    "url": url,
                    "headers": headers,
                    "payload": payload,
                    "timeout_sec": timeout_sec,
                }
            )
            return {"choices": [{"message": {"content": "{\"ok\": true}"}}]}

        import memoloupe.services.text_model as tm

        monkeypatch.setattr(tm, "http_json_post", fake_post)
        service, warning = build_text_model_service(
            {
                "textModel": {
                    "baseUrl": "https://api.example.test/v1/",
                    "apiKey": "sk-text",
                    "model": "story-model",
                    "timeoutSec": 12.5,
                    "maxTokens": 2048,
                }
            }
        )
        assert warning is None
        assert service is not None
        assert service.generate(TextModelRequest(task="story", prompt="hello")) == "{\"ok\": true}"
        assert captured["url"] == "https://api.example.test/v1/chat/completions"
        assert captured["timeout_sec"] == 12.5
        assert captured["payload"]["model"] == "story-model"
        assert captured["payload"]["max_tokens"] == 2048
        assert captured["headers"]["Authorization"] == "Bearer sk-text"
