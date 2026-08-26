"""CLI 文本模型配置适配（Phase 05-01）。

story/profile 共用同一套 ``textModel`` 配置。配置完整时构造真实
OpenAI-compatible 文本模型；配置缺失时返回 ``None``，由上层保持 scaffold /
skipped 显式降级。
"""

from __future__ import annotations

from typing import Any

from memoloupe.services.text_model import OpenAICompatibleTextModel, TextModelService


def build_text_model_service(config: dict[str, Any]) -> tuple[TextModelService | None, str | None]:
    """从 ``config["textModel"]`` 构造文本模型服务。

    返回 ``(service, warning)``：

    - 配置完整 → ``(OpenAICompatibleTextModel, None)``；
    - 完全未配置或部分缺失 → ``(None, warning)``，调用方继续降级。
    """
    raw = config.get("textModel")
    model_cfg = raw if isinstance(raw, dict) else {}
    base_url = _nonempty_str(model_cfg.get("baseUrl"))
    api_key = _nonempty_str(model_cfg.get("apiKey"))
    model = _nonempty_str(model_cfg.get("model"))
    missing = [
        name
        for name, value in (
            ("baseUrl", base_url),
            ("apiKey", api_key),
            ("model", model),
        )
        if value is None
    ]
    if missing:
        configured_any = any(
            _nonempty_str(model_cfg.get(name)) is not None
            for name in ("baseUrl", "apiKey", "model")
        )
        if configured_any:
            detail = "、".join(missing)
            return None, f"textModel 配置不完整（缺 {detail}），按无文本模型降级"
        return None, "textModel 未配置，按无文本模型降级"

    timeout = model_cfg.get("timeoutSec", 300.0)
    timeout_sec = float(timeout) if isinstance(timeout, (int, float)) else 300.0
    max_tokens_raw = model_cfg.get("maxTokens", 0)
    max_tokens = (
        int(max_tokens_raw)
        if isinstance(max_tokens_raw, int) and not isinstance(max_tokens_raw, bool) and max_tokens_raw > 0
        else None
    )
    return (
        OpenAICompatibleTextModel(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_sec=timeout_sec,
            max_tokens=max_tokens,
        ),
        None,
    )


def _nonempty_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
