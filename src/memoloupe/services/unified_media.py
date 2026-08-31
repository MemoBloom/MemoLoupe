"""UnifiedMLLM 服务端口（docs/01 §7.2、docs/03 §2.12）。

职责边界：本层只做 HTTP、鉴权、超时、请求构造与 **JSON 文本提取**。
fence 剥离、schema 校验、shotID 集合对齐、逐字段归一化都是编排器职责，
本层把模型文本原样返回。

日志遵循 docs/00 §7.3：只记录模型、镜头 ID、字节数、耗时、状态和脱敏错误；
绝不输出 API key、Authorization 头、视频 Data URI 或完整模型返回。
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

from memoloupe.core.errors import CapabilityUnavailableError
from memoloupe.core.logging import get_logger
from memoloupe.services.base import (
    SERVICE_PROTOCOL_VERSION,
    PermanentServiceError,
    http_json_post,
    redact_text,
)

__all__ = [
    "SERVICE_PROTOCOL_VERSION",
    "ModelClip",
    "AnalysisGroup",
    "UnifiedMediaService",
    "OpenAICompatibleUnifiedMedia",
]

_logger = get_logger("memoloupe.services.unified_media", phase="shot", step="unified")

_VIDEO_MIME = "video/mp4"
_TEMPERATURE = 0.0  # docs/03 §2.12：温度尽量低


@dataclass(frozen=True)
class ModelClip:
    """一个待分析的镜头模型代理 clip。"""

    shot_id: str
    proxy_path: Path  # clips/model-proxy/ 下的文件
    duration_ms: int


@dataclass(frozen=True)
class AnalysisGroup:
    """一组字段分析任务；prompt 已注入词表，fingerprint 由编排器计算。"""

    name: str  # "visual" | "audio" | "function"
    fields: tuple[str, ...]
    prompt: str
    schema: dict
    fingerprint: str


@runtime_checkable
class UnifiedMediaService(Protocol):
    def analyze_batch(
        self, clips: Sequence[ModelClip], group: AnalysisGroup
    ) -> str:
        """返回模型原始文本；结构化解析与校验由编排器负责。"""
        ...


class OpenAICompatibleUnifiedMedia:
    """OpenAI chat/completions 兼容的视频理解适配器。

    请求构造：每个 clip 读文件转 base64 ``video/mp4`` Data URI，作为
    ``video_url`` content part 与 prompt 一起放入单条 user 消息；视频 part
    携带 MiMo/OpenAI 兼容扩展 ``fps`` 与 ``media_resolution``；
    ``temperature`` 取最低（0.0），并声明 ``response_format=json_object``。

    文本提取顺序（docs/03 §2.12 的 1–2 步）：
    结构化字段（``message.parsed``）→ ``message.content``（字符串或 text parts）。
    后续 fence 剥离 / schema 校验 / shotID 对齐不在本层。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        fallback_model: str | None = None,
        timeout_sec: float = 300.0,
        video_fps: float = 10.0,
        media_resolution: str = "default",
        max_completion_tokens: int | None = None,
        thinking_mode: str | None = None,
    ) -> None:
        if not api_key:
            raise CapabilityUnavailableError("unifiedModel", "未配置 api_key")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._fallback_model = fallback_model
        self._timeout_sec = timeout_sec
        if not 0.1 <= float(video_fps) <= 10.0:
            raise ValueError("video_fps 必须在 [0.1, 10] 范围内")
        if media_resolution not in {"default", "max"}:
            raise ValueError("media_resolution 必须是 default 或 max")
        self._video_fps = float(video_fps)
        self._media_resolution = media_resolution
        if max_completion_tokens is not None and int(max_completion_tokens) < 1:
            raise ValueError("max_completion_tokens 必须为正整数或 None")
        if thinking_mode not in {None, "enabled", "disabled"}:
            raise ValueError("thinking_mode 必须是 enabled、disabled 或 None")
        self._max_completion_tokens = (
            int(max_completion_tokens) if max_completion_tokens is not None else None
        )
        self._thinking_mode = thinking_mode

    @property
    def model(self) -> str:
        """当前模型名（日志与 artifact 使用）。"""
        return self._model

    @property
    def fallback_model(self) -> str | None:
        """主模型持续不可用时编排器可回退的模型名。"""
        return self._fallback_model

    def with_model(self, model: str) -> "OpenAICompatibleUnifiedMedia":
        """返回以指定模型名重建的适配器（05-01B：per-request model override）。

        复用 base_url/api_key/timeout 与 fallback 配置，便于编排器在
        ``fallbackModel`` 上重发请求；协议 ``analyze_batch(clips, group)``
        保持不变。
        """
        return OpenAICompatibleUnifiedMedia(
            base_url=self._base_url,
            api_key=self._api_key,
            model=model,
            fallback_model=self._fallback_model,
            timeout_sec=self._timeout_sec,
            video_fps=self._video_fps,
            media_resolution=self._media_resolution,
            max_completion_tokens=self._max_completion_tokens,
            thinking_mode=self._thinking_mode,
        )

    def analyze_batch(
        self, clips: Sequence[ModelClip], group: AnalysisGroup
    ) -> str:
        # MiMo 官方视频示例以 video parts 在前、text part 在后；保持该顺序，
        # 同时把 fps/media_resolution 放在 video_url 的同级。
        content: list[dict] = []
        total_bytes = 0
        for clip in clips:
            try:
                data = clip.proxy_path.read_bytes()
            except OSError as exc:
                raise PermanentServiceError(
                    f"clip unreadable: shotID={clip.shot_id} {type(exc).__name__}"
                ) from None
            total_bytes += len(data)
            content.append(
                {
                    "type": "video_url",
                    "video_url": {
                        "url": f"data:{_VIDEO_MIME};base64,"
                        + base64.b64encode(data).decode("ascii")
                    },
                    "fps": self._video_fps,
                    "media_resolution": self._media_resolution,
                }
            )
        shot_mapping = "\n".join(
            f"- 第 {index} 个 video_url = {clip.shot_id}"
            for index, clip in enumerate(clips, 1)
        )
        request_prompt = (
            f"{group.prompt}\n"
            "本批次输入视频与 shotID 的唯一映射如下（严格按 content 顺序）：\n"
            f"{shot_mapping}\n"
            "不要把视频内部时间点当成镜头；shots 数组只能使用上述 shotID，"
            "并且每个 shotID 恰好返回一次。"
        )
        content.append({"type": "text", "text": request_prompt})
        payload = {
            "model": self._model,
            "temperature": _TEMPERATURE,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": content}],
        }
        if self._max_completion_tokens is not None:
            payload["max_completion_tokens"] = self._max_completion_tokens
        if self._thinking_mode is not None:
            payload["thinking"] = {"type": self._thinking_mode}
        shot_ids = [c.shot_id for c in clips]
        started = time.monotonic()
        try:
            response = http_json_post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                payload=payload,
                timeout_sec=self._timeout_sec,
            )
            text = self._extract_text(response)
        except Exception as exc:
            self._log(
                shot_ids,
                total_bytes,
                started,
                status="error",
                error=redact_text(str(exc), [self._api_key]),
            )
            raise
        self._log(shot_ids, total_bytes, started, status="ok")
        return text

    @staticmethod
    def _extract_text(response: dict) -> str:
        """按 docs/03 §2.12 顺序提取模型文本：结构化字段 → message content。"""
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict):
                parsed = message.get("parsed")
                if parsed is not None:
                    return json.dumps(parsed, ensure_ascii=False)
                content = message.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    parts = [
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict) and part.get("type") == "text"
                    ]
                    if parts:
                        return "".join(parts)
        raise PermanentServiceError("unified response: 无法提取模型文本")

    def _log(
        self,
        shot_ids: list[str],
        total_bytes: int,
        started: float,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        message = (
            f"unified request model={self._model} shots={','.join(shot_ids)} "
            f"bytes={total_bytes} status={status}"
        )
        if error:
            message += f" error={error}"
        _logger.debug(message, extra={"elapsedMs": str(elapsed_ms), "status": status})
