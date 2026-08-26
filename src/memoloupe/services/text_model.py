"""通用文本模型服务端口（docs/01 §7、roadmap 03-03）。

服务于 story 叙事字段填充与后续 profile 蒸馏：接受结构化请求，返回原始
JSON 文本（解析/校验由调用方负责）。OpenAI-compatible 适配器复用
:mod:`memoloupe.services.base` 的 HTTP、鉴权与脱敏逻辑。

安全不变量：本层只发送调用方构造的文本 prompt；story/profile 的 prompt
构造方必须保证不含视频、帧、Data URI 或本地路径（由编排层测试断言）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from memoloupe.core.errors import CapabilityUnavailableError
from memoloupe.services.base import (
    SERVICE_PROTOCOL_VERSION,
    PermanentServiceError,
    http_json_post,
)

__all__ = [
    "SERVICE_PROTOCOL_VERSION",
    "TextModelRequest",
    "TextModelService",
    "OpenAICompatibleTextModel",
]


@dataclass(frozen=True)
class TextModelRequest:
    """一次文本生成请求。

    - ``task``：任务名（如 ``story-narrative``），进入指纹与日志；
    - ``prompt``：已渲染的纯文本 prompt；
    - ``system``：可选系统提示；
    - ``max_tokens``：可选输出上限（None 时由服务端默认）。
    """

    task: str
    prompt: str
    system: str | None = None
    max_tokens: int | None = None


@runtime_checkable
class TextModelService(Protocol):
    """文本模型端口：返回原始 JSON 文本，可能抛 Transient/PermanentServiceError。"""

    def generate(self, request: TextModelRequest) -> str: ...


class OpenAICompatibleTextModel:
    """OpenAI 兼容 chat/completions 文本模型适配器。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout_sec: float = 300.0,
        max_tokens: int | None = None,
    ) -> None:
        if not api_key:
            raise CapabilityUnavailableError("textModel", "未配置 api_key")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_sec = timeout_sec
        self._max_tokens = max_tokens

    def generate(self, request: TextModelRequest) -> str:
        messages: list[dict] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})
        payload: dict = {"model": self._model, "messages": messages}
        max_tokens = request.max_tokens if request.max_tokens is not None else self._max_tokens
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        response = http_json_post(
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            payload=payload,
            timeout_sec=self._timeout_sec,
        )
        choices = response.get("choices")
        if (
            not isinstance(choices, list)
            or not choices
            or not isinstance(choices[0], dict)
        ):
            raise PermanentServiceError("text model response: 缺 choices")
        message = choices[0].get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise PermanentServiceError("text model response: message.content 为空")
        return content
