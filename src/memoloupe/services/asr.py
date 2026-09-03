"""ASR 服务端口（docs/01 §7.1、docs/03 §2.7）。

适配器把供应商响应归一为稳定结构，供应商扩展只进入 ``raw_extras`` 命名空间，
绝不泄漏为主契约字段。05-01C：支持两种 transport——``openai-json``
（JSON + base64，默认）与 ``openai-multipart``（multipart/file 上传，
原版 memoclip-lapian 形态）；:func:`build_asr_service` 按 ``asr.provider``
构造。
"""

from __future__ import annotations

import base64
import importlib.util
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from memoloupe.core.errors import CapabilityUnavailableError
from memoloupe.core.logging import get_logger
from memoloupe.core.time_ranges import seconds_to_ms
from memoloupe.services.base import (
    SERVICE_PROTOCOL_VERSION,
    PermanentServiceError,
    http_json_post,
    http_post_bytes,
)

_logger = get_logger("memoloupe.services.asr", phase="shot", step="asr")

__all__ = [
    "SERVICE_PROTOCOL_VERSION",
    "ASRRequest",
    "ASRResult",
    "ASRService",
    "OpenAICompatibleASR",
    "MultipartOpenAICompatibleASR",
    "build_asr_service",
    "local_asr_available",
    "PROVIDER_AUTO",
]

#: 支持的 provider / transport。
PROVIDER_JSON = "openai-json"
PROVIDER_MULTIPART = "openai-multipart"
PROVIDER_LOCAL = "local-fireredvad-mlx"
#: connect-first：自动路由——本地依赖优先，远程 provider 兜底，否则显式降级。
PROVIDER_AUTO = "auto"


def local_asr_available() -> bool:
    """本地 ASR 可选依赖（fireredvad / mlx-whisper）是否已安装。"""
    return (
        importlib.util.find_spec("fireredvad") is not None
        and importlib.util.find_spec("mlx_whisper") is not None
    )


def _build_local_asr(config: dict, asr_cfg: dict) -> ASRService:
    """构造本地 FireRedVAD + MLX Whisper ASR（依赖缺失在 transcribe 时降级）。"""
    from memoloupe.services.asr_local import LocalFireRedVadMlxASR

    ffmpeg_cfg = config.get("ffmpeg", {}) if isinstance(config, dict) else {}
    return LocalFireRedVadMlxASR(
        asr_config=asr_cfg,
        ffmpeg_path=str(ffmpeg_cfg.get("ffmpegPath", "ffmpeg")),
        decode_timeout_sec=float(ffmpeg_cfg.get("scanTimeoutSec", 600.0)),
    )


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


def _multipart_body(
    fields: dict[str, str],
    file_field: str,
    filename: str,
    file_bytes: bytes,
    content_type: str,
) -> tuple[bytes, str]:
    """手工构造 multipart/form-data body，返回 ``(body, boundary)``。"""
    boundary = f"----MemoLoupeBoundary{uuid.uuid4().hex}"
    lines: list[bytes] = []
    for name, value in fields.items():
        lines.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n'
            f"\r\n{value}\r\n".encode("utf-8")
        )
    lines.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_field}"; '
        f'filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
    )
    lines.append(file_bytes)
    lines.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(lines), boundary


class MultipartOpenAICompatibleASR:
    """OpenAI ``/audio/transcriptions`` multipart/file 上传适配器。

    原版 memoclip-lapian 使用 multipart 文件上传；本适配器用标准库手工构造
    multipart body（不引入第三方依赖），文件字段名由 ``file_field`` 配置
    （默认 ``file``）。响应归一化与 :class:`OpenAICompatibleASR` 相同。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        file_field: str = "file",
        timeout_sec: float = 120.0,
    ) -> None:
        if not api_key:
            raise CapabilityUnavailableError("asr", "未配置 api_key")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._file_field = file_field
        self._timeout_sec = timeout_sec

    def transcribe(self, media_path: Path, request: ASRRequest) -> ASRResult:
        try:
            audio_bytes = media_path.read_bytes()
        except OSError as exc:
            raise PermanentServiceError(
                f"asr media unreadable: {type(exc).__name__}"
            ) from None
        fields = {"model": self._model, "response_format": "verbose_json"}
        if request.language:
            fields["language"] = request.language
        body, boundary = _multipart_body(
            fields,
            file_field=self._file_field,
            filename=media_path.name,
            file_bytes=audio_bytes,
            content_type="application/octet-stream",
        )
        response = http_post_bytes(
            f"{self._base_url}/audio/transcriptions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            body=body,
            timeout_sec=self._timeout_sec,
        )
        return self._normalize(response, request)

    def _normalize(self, response: dict, request: ASRRequest) -> ASRResult:
        """与 OpenAICompatibleASR 同一归一化（segments → 稳定结构）。"""
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


def build_asr_service(config: dict) -> ASRService | None:
    """按 ``config["asr"]`` 构造 ASR 服务；未配置/未启用时返回 None。

    ``provider`` 取值：``openai-json``（默认，JSON + base64）、
    ``openai-multipart``（multipart 文件上传，``fileField`` 可配置）、
    ``local-fireredvad-mlx``（本地 FireRedVAD + MLX Whisper）、
    ``auto``（本地依赖可用则本地，否则远程三项齐全走远程，皆无则显式降级）。
    """
    asr_cfg = config.get("asr", {}) if isinstance(config, dict) else {}
    if not asr_cfg.get("enabled", True):
        return None
    provider = str(asr_cfg.get("provider", PROVIDER_JSON))
    if provider == PROVIDER_LOCAL:
        return _build_local_asr(config, asr_cfg)
    api_key = asr_cfg.get("apiKey")
    base_url = asr_cfg.get("baseUrl")
    model = asr_cfg.get("model")
    if provider == PROVIDER_AUTO:
        if local_asr_available():
            return _build_local_asr(config, asr_cfg)
        if not (api_key and base_url and model):
            # 不静默跳过：说明降级原因并给出下一步（connect / 本地依赖）。
            _logger.warning(
                "ASR auto 路由：本地依赖（fireredvad/mlx-whisper）不可用且远程 "
                "ASR 未配置，ASR 将显式降级。可运行 memoloupe connect add qwen "
                "连接 provider，或安装本地依赖：uv sync --extra asr-local"
            )
            return None
        # 远程三项齐全：按默认 JSON transport 构造（落到下方公共分支）。
    if not (api_key and base_url and model):
        return None
    timeout_sec = float(asr_cfg.get("timeoutSec", 120.0))
    if provider == PROVIDER_MULTIPART:
        return MultipartOpenAICompatibleASR(
            base_url=str(base_url),
            api_key=str(api_key),
            model=str(model),
            file_field=str(asr_cfg.get("fileField", "file")),
            timeout_sec=timeout_sec,
        )
    return OpenAICompatibleASR(
        base_url=str(base_url),
        api_key=str(api_key),
        model=str(model),
        timeout_sec=timeout_sec,
    )
