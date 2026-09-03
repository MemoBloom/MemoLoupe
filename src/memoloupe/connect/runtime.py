"""active provider → 管道配置叠加（connect-first Task 4）。

解析优先级：

1. connections.json 有 active provider → 用其 baseUrl/模型与 SecretStore
   凭据覆盖 ``unifiedModel``/``textModel`` 的连接三要素（provider 声明 ASR
   能力且配置了 ASR 模型时同步覆盖 ``asr`` 的三要素；record 携带
   ``asrTransport`` 时同步覆盖 ``asr.provider``，未携带则保留原 transport），
   返回 source="provider"；
2. 无 active provider 但 env/配置文件已把 ``unifiedModel`` 或
   ``textModel`` 配齐（baseUrl/apiKey/model 均非空）→ 原样返回，
   source="env"；
3. 两者皆无 → 原样返回，source="none"，由调用方显式降级并给出
   ``memoloupe connect add`` 引导。

凭据缺失不算"无 provider"：连接已建立却取不到 key 是显式错误，
抛 :class:`ConnectionStoreError` 并指向 connect test / connect add；
错误信息绝不包含凭据本身。
"""

from __future__ import annotations

import copy

from memoloupe.connect.secrets import SecretStore, default_secret_store
from memoloupe.connect.store import (
    ConnectionStore,
    ConnectionStoreError,
)

#: 配置来源标记。
SOURCE_PROVIDER = "provider"
SOURCE_ENV = "env"
SOURCE_NONE = "none"

#: 远程服务连接三要素。
_CONNECTION_KEYS = ("baseUrl", "apiKey", "model")


def _is_complete_remote(group: object) -> bool:
    """分组是否配齐远程服务三要素（非空字符串）。"""
    if not isinstance(group, dict):
        return False
    return all(
        isinstance(group.get(key), str) and bool(group[key].strip())
        for key in _CONNECTION_KEYS
    )


def resolve_active_provider(
    config: dict,
    *,
    store: ConnectionStore | None = None,
    secrets: SecretStore | None = None,
) -> tuple[dict, str]:
    """把 active provider 叠加到 ``config``，返回 ``(新配置, source)``。

    不修改入参：provider 叠加发生在深拷贝上；无 provider 时返回的 config
    与入参内容相同。
    """
    if store is None:
        store = ConnectionStore()
    if secrets is None:
        secrets = default_secret_store()

    record = store.get_active()
    if record is None:
        source = (
            SOURCE_ENV
            if _is_complete_remote(config.get("unifiedModel"))
            or _is_complete_remote(config.get("textModel"))
            else SOURCE_NONE
        )
        return config, source

    provider_id = record["providerId"]
    api_key = secrets.get(provider_id)
    if not api_key:
        raise ConnectionStoreError(
            f"当前 provider {provider_id!r} 缺少凭据。请运行 "
            f"memoloupe connect test {provider_id} 检查连接状态，或重新运行 "
            f"memoloupe connect add {provider_id} 保存凭据"
        )

    resolved = copy.deepcopy(config)
    base_url = record["baseUrl"]
    models = record["models"]
    resolved.setdefault("unifiedModel", {}).update(
        {"baseUrl": base_url, "apiKey": api_key, "model": models["media"]}
    )
    resolved.setdefault("textModel", {}).update(
        {"baseUrl": base_url, "apiKey": api_key, "model": models["text"]}
    )
    capabilities = record.get("capabilities", {})
    asr_model = models.get("asr")
    if capabilities.get("asr") and isinstance(asr_model, str) and asr_model:
        asr_overlay: dict = {"baseUrl": base_url, "apiKey": api_key, "model": asr_model}
        # record 声明了 ASR transport（如 mimo-chat）时同步覆盖 asr.provider；
        # 未声明时保留原值（asr.provider 默认 openai-json）。
        transport = record.get("asrTransport")
        if isinstance(transport, str) and transport:
            asr_overlay["provider"] = transport
        resolved.setdefault("asr", {}).update(asr_overlay)
    return resolved, SOURCE_PROVIDER
