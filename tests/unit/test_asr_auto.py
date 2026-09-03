"""``asr.provider = "auto"`` 路由（connect-first Task 5）。

路由顺序：本地依赖可用 → ``LocalFireRedVadMlxASR``；远程三项齐全 →
现有远程构造；两者皆无 → ``None`` + 非静默 warning（含 connect 引导）。
既有 provider 值（openai-json/openai-multipart/local-fireredvad-mlx）
行为不变。
"""

from __future__ import annotations

import logging

import pytest

import memoloupe.services.asr as asr_mod
from memoloupe.services.asr import (
    PROVIDER_JSON,
    PROVIDER_MULTIPART,
    MultipartOpenAICompatibleASR,
    OpenAICompatibleASR,
    build_asr_service,
)
from memoloupe.services.asr_local import LocalFireRedVadMlxASR


def _config(**overrides) -> dict:
    cfg = {
        "enabled": True,
        "provider": "auto",
        "baseUrl": "http://asr.example.com",
        "apiKey": "sk-key",
        "model": "whisper-x",
        "timeoutSec": 30.0,
    }
    cfg.update(overrides)
    return {"asr": cfg, "ffmpeg": {}}


@pytest.fixture
def local_available(monkeypatch):
    monkeypatch.setattr(asr_mod, "local_asr_available", lambda: True)


@pytest.fixture
def local_unavailable(monkeypatch):
    monkeypatch.setattr(asr_mod, "local_asr_available", lambda: False)


class TestAutoRouting:
    def test_local_preferred_when_available(self, local_available) -> None:
        service = build_asr_service(_config())
        assert isinstance(service, LocalFireRedVadMlxASR)

    def test_remote_fallback_when_local_unavailable(self, local_unavailable) -> None:
        service = build_asr_service(_config())
        assert isinstance(service, OpenAICompatibleASR)

    def test_neither_returns_none_with_warning(
        self, local_unavailable, caplog
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="memoloupe.services.asr"):
            service = build_asr_service(
                _config(baseUrl=None, apiKey=None, model=None)
            )
        assert service is None
        messages = [record.getMessage() for record in caplog.records]
        assert any("connect add" in message for message in messages)

    def test_disabled_still_returns_none(self, local_available) -> None:
        assert build_asr_service(_config(enabled=False)) is None


class TestExplicitProvidersUnchanged:
    """既有显式 provider 行为回归（不受 auto 路由影响）。"""

    def test_json_default(self) -> None:
        service = build_asr_service(_config(provider=PROVIDER_JSON))
        assert isinstance(service, OpenAICompatibleASR)

    def test_multipart(self) -> None:
        service = build_asr_service(_config(provider=PROVIDER_MULTIPART))
        assert isinstance(service, MultipartOpenAICompatibleASR)

    def test_local_explicit(self, monkeypatch) -> None:
        # 显式 local provider 不做依赖探测，直接构造（依赖缺失在 transcribe 时降级）。
        monkeypatch.setattr(asr_mod, "local_asr_available", lambda: False)
        service = build_asr_service(_config(provider="local-fireredvad-mlx"))
        assert isinstance(service, LocalFireRedVadMlxASR)

    def test_missing_credentials_returns_none(self) -> None:
        assert build_asr_service(_config(provider=PROVIDER_JSON, apiKey=None)) is None
