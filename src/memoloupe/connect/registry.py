"""provider 注册表：内置 provider 的默认值与查找。

纯数据模块；新增 provider 只需在 ``PROVIDERS`` 中追加一条 ProviderSpec。
"""

from __future__ import annotations

from dataclasses import dataclass

from memoloupe.connect.store import ConnectionStoreError


@dataclass(frozen=True)
class ProviderSpec:
    """单个 provider 的内置默认配置。"""

    provider_id: str
    label: str
    default_base_url: str
    default_media_model: str
    default_text_model: str
    default_asr_model: str | None
    capabilities: dict[str, bool]  # mediaUnderstanding / text / asr
    health_check_path: str  # OpenAI 兼容端点，固定 "/models"
    #: ASR transport（services.asr 的 provider 值）；None 表示无 ASR 能力。
    asr_transport: str | None = None


PROVIDERS: dict[str, ProviderSpec] = {
    "qwen": ProviderSpec(
        provider_id="qwen",
        label="通义千问（DashScope 兼容模式）",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_media_model="qwen3.5-omni",
        default_text_model="qwen-plus",
        default_asr_model=None,
        capabilities={"mediaUnderstanding": True, "text": True, "asr": False},
        health_check_path="/models",
    ),
    "mimo": ProviderSpec(
        provider_id="mimo",
        label="小米 MiMo",
        default_base_url="https://api.xiaomimimo.com/v1",
        default_media_model="mimo-v2.5",
        default_text_model="mimo-v2.5",
        # MiMo ASR（mimo-v2.5-asr）走 chat/completions + input_audio（D-057）。
        default_asr_model="mimo-v2.5-asr",
        capabilities={"mediaUnderstanding": True, "text": True, "asr": True},
        health_check_path="/models",
        asr_transport="mimo-chat",
    ),
}


def get_provider_spec(provider_id: str) -> ProviderSpec:
    """按 id 查 provider 规格；未知 id 抛 :class:`ConnectionStoreError`。"""
    try:
        return PROVIDERS[provider_id]
    except KeyError:
        supported = ", ".join(sorted(PROVIDERS))
        raise ConnectionStoreError(
            f"未知 provider {provider_id!r}；支持的 provider：{supported}"
        ) from None
