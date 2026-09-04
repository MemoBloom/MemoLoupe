"""切点语义模型服务端口（Phase 06-04）。

默认实现复用 :class:`OpenAICompatibleTextModel`（当前即小米 MiMo
``mimo-v2.5``，经 connect/textModel 配置注入）。模型只做语义判断，
不得修改镜头边界、pair 集合或确定性指标（docs 计划 §4.3 白名单）。
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from memoloupe.analysis.shot_relation_prompts import (
    render_pair_prompt,
)
from memoloupe.services.base import TransientServiceError
from memoloupe.services.text_model import TextModelRequest

SEMANTIC_TASK = "shot-relation"


class ShotRelationSemanticsService(Protocol):
    """切点语义端口：输入白名单 payload，返回模型原始 JSON 文本。"""

    def analyze_pair(self, payload: dict) -> str: ...


class TextModelShotRelationService:
    """基于现有 TextModelService（MiMo）的切点语义实现。"""

    def __init__(
        self,
        text_service: Any,
        *,
        model_name: str | None = None,
        retries: int = 1,
    ) -> None:
        self._text_service = text_service
        self._model_name = model_name
        self._retries = max(int(retries), 0)

    def marker(self) -> str:
        """指纹/日志用服务标记（不含凭据）。"""
        return f"text-model:{self._model_name or 'unknown'}"

    def analyze_pair(self, payload: dict) -> str:
        prompt = render_pair_prompt(payload)
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                return self._text_service.generate(
                    TextModelRequest(task=SEMANTIC_TASK, prompt=prompt)
                )
            except TransientServiceError as exc:
                last_error = exc
                if attempt < self._retries:
                    time.sleep(2.0 * (attempt + 1))
        raise last_error  # type: ignore[misc]


def build_shot_relation_service(config: dict) -> TextModelShotRelationService | None:
    """按 ``textModel`` 配置构造语义服务；未配置三要素时返回 None。"""
    cfg = config.get("textModel", {})
    api_key = cfg.get("apiKey")
    base_url = cfg.get("baseUrl")
    model = cfg.get("model")
    if not (api_key and base_url and model):
        return None
    from memoloupe.services.text_model import OpenAICompatibleTextModel

    text_service = OpenAICompatibleTextModel(
        base_url=str(base_url),
        api_key=str(api_key),
        model=str(model),
        timeout_sec=float(cfg.get("timeoutSec", 300.0)),
        max_tokens=int(cfg.get("maxTokens", 0)) or None,
    )
    return TextModelShotRelationService(text_service, model_name=str(model))