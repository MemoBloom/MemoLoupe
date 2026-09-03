"""MiMo chat ASR（provider=mimo-chat，mimo-v2.5-asr）单元测试。

MiMo ASR 走 ``/chat/completions``（input_audio data URL），响应为纯文本、
**不含时间戳**；客户端按固定窗口切片，segment 时间取窗口边界（确定性事实）。
"""

from __future__ import annotations

import base64
import io
import wave
from pathlib import Path

import pytest

import memoloupe.services.asr as asr_mod
from memoloupe.services.asr import (
    MiMoChatASR,
    PROVIDER_MIMO_CHAT,
    ASRRequest,
    build_asr_service,
)
from memoloupe.core.errors import CapabilityUnavailableError
from memoloupe.services.base import PermanentServiceError

KEY = "sk-mimo-test-key"


def _wav_bytes(duration_ms: int, sample_rate: int = 16000) -> bytes:
    """生成指定时长的静音 16kHz mono s16le wav。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * (sample_rate * duration_ms // 1000))
    return buf.getvalue()


@pytest.fixture
def fake_decode(monkeypatch):
    """拦截 ffmpeg 解码：按请求区间长度写静音 wav。"""

    def fake(ffmpeg_path, media_path, start_ms, end_ms, out_path, timeout_sec):
        duration = int((end_ms or 60_000) - start_ms)
        Path(out_path).write_bytes(_wav_bytes(max(duration, 1)))

    monkeypatch.setattr(asr_mod, "_decode_wav_range", fake)


@pytest.fixture
def fake_http(monkeypatch):
    """拦截 http_json_post：按调用序号返回文本，记录 payload。"""
    calls: list[dict] = {"payloads": [], "texts": ["第一段歌词", "第二段歌词"]}

    def fake_post(url, *, headers, payload, timeout_sec):
        calls["payloads"].append(payload)
        calls["url"] = url
        calls["headers"] = headers
        index = len(calls["payloads"]) - 1
        text = calls["texts"][index] if index < len(calls["texts"]) else ""
        return {
            "choices": [
                {"finish_reason": "stop", "message": {"role": "assistant", "content": text}}
            ],
            "usage": {"seconds": 30},
        }

    monkeypatch.setattr(asr_mod, "http_json_post", fake_post)
    return calls


def _service(**overrides) -> MiMoChatASR:
    kwargs = {
        "base_url": "https://api.xiaomimimo.com/v1",
        "api_key": KEY,
        "model": "mimo-v2.5-asr",
        "window_sec": 30,
    }
    kwargs.update(overrides)
    return MiMoChatASR(**kwargs)


class TestMiMoChatASR:
    def test_windowing_and_absolute_segment_times(self, fake_decode, fake_http, tmp_path):
        # 50 秒音频、30s 窗口、请求起点 5000ms → 两个 segment，时间为绝对时间轴。
        service = _service()
        result = service.transcribe(
            tmp_path / "v.mp4", ASRRequest(language=None, start_ms=5000, end_ms=55_000)
        )
        assert [(s["startMs"], s["endMs"], s["text"]) for s in result.segments] == [
            (5000, 35_000, "第一段歌词"),
            (35_000, 55_000, "第二段歌词"),
        ]

    def test_request_payload_shape(self, fake_decode, fake_http, tmp_path):
        service = _service()
        service.transcribe(tmp_path / "v.mp4", ASRRequest(language="zh", start_ms=0, end_ms=10_000))
        payload = fake_http["payloads"][0]
        assert fake_http["url"] == "https://api.xiaomimimo.com/v1/chat/completions"
        assert fake_http["headers"]["Authorization"] == f"Bearer {KEY}"
        assert payload["model"] == "mimo-v2.5-asr"
        assert payload["asr_options"] == {"language": "zh"}
        part = payload["messages"][0]["content"][0]
        assert part["type"] == "input_audio"
        data_url = part["input_audio"]["data"]
        assert data_url.startswith("data:audio/wav;base64,")
        base64.b64decode(data_url.split(",", 1)[1])  # 合法 base64

    def test_language_defaults_to_auto(self, fake_decode, fake_http, tmp_path):
        _service().transcribe(tmp_path / "v.mp4", ASRRequest(start_ms=0, end_ms=10_000))
        assert fake_http["payloads"][0]["asr_options"] == {"language": "auto"}

    def test_empty_text_window_produces_no_segment(self, fake_decode, fake_http, tmp_path):
        fake_http["texts"] = ["有歌词", ""]
        result = _service().transcribe(tmp_path / "v.mp4", ASRRequest(start_ms=0, end_ms=50_000))
        assert [s["text"] for s in result.segments] == ["有歌词"]

    def test_window_times_are_recorded_as_client_side_fact(self, fake_decode, fake_http, tmp_path):
        result = _service().transcribe(tmp_path / "v.mp4", ASRRequest(start_ms=0, end_ms=10_000))
        assert result.raw_extras["provider"]["windowed"] is True
        assert result.raw_extras["provider"]["windowMs"] == 30_000

    def test_bad_response_raises_permanent(self, fake_decode, monkeypatch, tmp_path):
        monkeypatch.setattr(
            asr_mod, "http_json_post",
            lambda url, *, headers, payload, timeout_sec: {"choices": []},
        )
        with pytest.raises(PermanentServiceError):
            _service().transcribe(tmp_path / "v.mp4", ASRRequest(start_ms=0, end_ms=10_000))

    def test_requires_api_key(self):
        with pytest.raises(CapabilityUnavailableError):
            _service(api_key=None)


class TestBuildServiceMiMoChat:
    def _config(self, **overrides) -> dict:
        cfg = {
            "enabled": True,
            "provider": PROVIDER_MIMO_CHAT,
            "baseUrl": "https://api.xiaomimimo.com/v1",
            "apiKey": KEY,
            "model": "mimo-v2.5-asr",
        }
        cfg.update(overrides)
        return {"asr": cfg, "ffmpeg": {}}

    def test_builds_mimo_chat_adapter(self):
        service = build_asr_service(self._config())
        assert isinstance(service, MiMoChatASR)

    def test_missing_credentials_returns_none(self):
        assert build_asr_service(self._config(apiKey=None)) is None
