"""ASR 服务端口（docs/01 §7.1、docs/03 §2.7）。

适配器把供应商响应归一为稳定结构，供应商扩展只进入 ``raw_extras`` 命名空间，
绝不泄漏为主契约字段。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from memoloupe.core.errors import CapabilityUnavailableError
from memoloupe.core.time_ranges import seconds_to_ms
from memoloupe.services.base import (
    SERVICE_PROTOCOL_VERSION,
    PermanentServiceError,
    http_json_post,
)

__all__ = [
    "SERVICE_PROTOCOL_VERSION",
    "ASRRequest",
    "ASRResult",
    "ASRService",
    "OpenAICompatibleASR",
]


@dataclass(frozen=True)
class ASRRequest:
    """一次转写请求；时间窗为半开区间 ``[start_ms, end_ms)``。"""

    language: str | None = None
    start_ms: int = 0
    end_ms: int | None = None


@dataclass(frozen=True)
class ASRResult:
    """归一化转写结果。

    ``segments`` 每项为 ``{startMs, endMs, text, speaker, confidence}``；
    ``raw_extras`` 以命名空间（如 ``provider``）隔离供应商扩展字段。
    """

    segments: tuple[dict, ...]
    raw_extras: dict = field(default_factory=dict)


@runtime_checkable
class ASRService(Protocol):
    def transcribe(self, media_path: Path, request: ASRRequest) -> ASRResult: ...


class OpenAICompatibleASR:
    """OpenAI 兼容 ASR 适配器。

    请求格式说明：OpenAI 官方的 ``/audio/transcriptions`` 是 multipart/form-data，
    但 multipart 用标准库手工构造过于繁琐且容易出错，因此本适配器采用
    **JSON + base64** 请求体：``{"model", "audio_base64", "audio_format",
    "response_format": "verbose_json", "language"?}``。这是与代理/自建网关在
    OpenAI 兼容语义下的一种常见扩展；若目标供应商只接受 multipart，
    需要在本层再包一层转换适配器，稳定契约（ASRRequest/ASRResult）不变。

    响应按 ``verbose_json`` 的 ``segments`` 归一化：秒 → 毫秒统一走
    :func:`memoloupe.core.time_ranges.seconds_to_ms`；缺 ``speaker`` /
    ``confidence`` 一律补 ``None``（JSON null）。请求时间窗在客户端按
    正交集过滤 segments（JSON+base64 格式下服务端不一定支持裁剪）。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout_sec: float = 120.0,
    ) -> None:
        if not api_key:
            raise CapabilityUnavailableError("asr", "未配置 api_key")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_sec = timeout_sec

    def transcribe(self, media_path: Path, request: ASRRequest) -> ASRResult:
        try:
            audio_bytes = media_path.read_bytes()
        except OSError as exc:
            raise PermanentServiceError(
                f"asr media unreadable: {type(exc).__name__}"
            ) from None
        payload: dict = {
            "model": self._model,
            "response_format": "verbose_json",
            "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
            "audio_format": media_path.suffix.lstrip(".") or "mp4",
        }
        if request.language:
            payload["language"] = request.language
        response = http_json_post(
            f"{self._base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            payload=payload,
            timeout_sec=self._timeout_sec,
        )
        return self._normalize(response, request)

    def _normalize(self, response: dict, request: ASRRequest) -> ASRResult:
        raw_segments = response.get("segments", [])
        if not isinstance(raw_segments, list):
            raise PermanentServiceError("asr response: segments is not a list")
        segments: list[dict] = []
        for index, seg in enumerate(raw_segments):
            if not isinstance(seg, dict) or not {"start", "end", "text"} <= seg.keys():
                raise PermanentServiceError(
                    f"asr response: segment[{index}] 缺少 start/end/text"
                )
            start_ms = seconds_to_ms(seg["start"])
            end_ms = seconds_to_ms(seg["end"])
            # 按请求时间窗做正交集过滤（docs/03 §2.7 的镜头对齐在解析层做）。
            if end_ms <= request.start_ms:
                continue
            if request.end_ms is not None and start_ms >= request.end_ms:
                continue
            segments.append(
                {
                    "startMs": start_ms,
                    "endMs": end_ms,
                    "text": str(seg["text"]),
                    "speaker": seg.get("speaker"),
                    "confidence": seg.get("confidence"),
                }
            )
        raw_extras = {
            "provider": {k: v for k, v in response.items() if k != "segments"}
        }
        return ASRResult(segments=tuple(segments), raw_extras=raw_extras)
