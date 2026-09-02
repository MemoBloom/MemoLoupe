"""连接存储：connections.json 的加载、校验与原子写。

connections.json 只存非机密连接信息（baseUrl、模型选择、能力开关）；
API key 永远不进该文件，由 :mod:`memoloupe.connect.secrets` 单独保管。
文件权限固定为 0o600。
"""

from __future__ import annotations

import os
from pathlib import Path

from memoloupe.core.atomic_io import read_json, write_json_atomic
from memoloupe.core.errors import MemoLoupeError

CONNECTIONS_VERSION = 1

# provider record 的必填字段；models 内 media/text 必填，asr 允许为 None。
_REQUIRED_RECORD_FIELDS = ("providerId", "baseUrl", "models", "capabilities")
_REQUIRED_MODEL_KEYS = ("media", "text")


class ConnectionStoreError(MemoLoupeError):
    """连接存储的读写或校验失败。"""


def default_connections_path() -> Path:
    """默认连接文件路径；``MEMOLOUPE_CONNECTIONS_PATH`` 环境变量可覆盖。"""
    override = os.environ.get("MEMOLOUPE_CONNECTIONS_PATH")
    if override:
        return Path(override)
    return Path.home() / ".config" / "memoloupe" / "connections.json"


def _empty_skeleton() -> dict:
    return {"version": CONNECTIONS_VERSION, "activeProvider": None, "providers": {}}


def _validate_record(record: dict) -> None:
    """校验单个 provider record；任何显式拒绝都在这里抛出。"""
    if not isinstance(record, dict):
        raise ConnectionStoreError(f"provider record 必须是对象，实际为 {type(record).__name__}")
    if "apiKey" in record:
        raise ConnectionStoreError(
            "provider record 不允许包含 apiKey；凭据请使用 SecretStore 单独存储"
        )
    for field in _REQUIRED_RECORD_FIELDS:
        if field not in record:
            raise ConnectionStoreError(f"provider record 缺少必填字段 {field!r}")
    provider_id = record["providerId"]
    # 延迟导入避免循环：registry 的未知 id 错误也复用 ConnectionStoreError。
    from memoloupe.connect.registry import PROVIDERS

    if provider_id not in PROVIDERS:
        supported = ", ".join(sorted(PROVIDERS))
        raise ConnectionStoreError(
            f"未知 providerId {provider_id!r}；支持的 provider：{supported}"
        )
    if not isinstance(record["baseUrl"], str) or not record["baseUrl"]:
        raise ConnectionStoreError("provider record 的 baseUrl 必须是非空字符串")
    models = record["models"]
    if not isinstance(models, dict):
        raise ConnectionStoreError("provider record 的 models 必须是对象")
    for key in _REQUIRED_MODEL_KEYS:
        if key not in models or not isinstance(models[key], str) or not models[key]:
            raise ConnectionStoreError(f"provider record 的 models 缺少非空字符串键 {key!r}")
    if "asr" in models and models["asr"] is not None and not isinstance(models["asr"], str):
        raise ConnectionStoreError("provider record 的 models.asr 必须是字符串或 None")
    if not isinstance(record["capabilities"], dict):
        raise ConnectionStoreError("provider record 的 capabilities 必须是对象")


def _validate(data: dict) -> None:
    """校验整份 connections 数据；集中所有显式拒绝。"""
    if not isinstance(data, dict):
        raise ConnectionStoreError("connections 数据必须是 JSON 对象")
    version = data.get("version")
    if version != CONNECTIONS_VERSION:
        raise ConnectionStoreError(
            f"不支持的 connections 版本 {version!r}；期望 {CONNECTIONS_VERSION}"
        )
    providers = data.get("providers")
    if not isinstance(providers, dict):
        raise ConnectionStoreError("connections 数据缺少 providers 对象")
    for key, record in providers.items():
        _validate_record(record)
        if record["providerId"] != key:
            raise ConnectionStoreError(
                f"providers 键 {key!r} 与 record.providerId {record['providerId']!r} 不一致"
            )
    active = data.get("activeProvider")
    if active is not None and active not in providers:
        raise ConnectionStoreError(
            f"activeProvider {active!r} 指向不存在的 provider"
        )


class ConnectionStore:
    """connections.json 的读写入口。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else default_connections_path()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict:
        """加载连接数据；文件不存在时返回空骨架。"""
        if not self._path.exists():
            return _empty_skeleton()
        try:
            data = read_json(self._path)
        except MemoLoupeError as exc:
            raise ConnectionStoreError(
                f"connections 文件无法解析：{self._path}（{exc}）"
            ) from exc
        _validate(data)
        return data

    def save(self, data: dict) -> None:
        """校验并原子写入；写后把权限收紧到 0o600。"""
        _validate(data)
        write_json_atomic(self._path, data)
        os.chmod(self._path, 0o600)

    def upsert_provider(self, record: dict, *, make_active: bool) -> None:
        """新增或更新一个 provider record。"""
        _validate_record(record)
        data = self.load()
        data["providers"][record["providerId"]] = record
        if make_active:
            data["activeProvider"] = record["providerId"]
        self.save(data)

    def remove_provider(self, provider_id: str) -> None:
        """删除 provider；若它是当前 active，同时清空 activeProvider。"""
        data = self.load()
        if provider_id not in data["providers"]:
            raise ConnectionStoreError(f"provider {provider_id!r} 不存在，无法删除")
        del data["providers"][provider_id]
        if data["activeProvider"] == provider_id:
            data["activeProvider"] = None
        self.save(data)

    def set_active(self, provider_id: str) -> None:
        """把已存在的 provider 设为 active。"""
        data = self.load()
        if provider_id not in data["providers"]:
            raise ConnectionStoreError(
                f"provider {provider_id!r} 不存在，无法设为 active"
            )
        data["activeProvider"] = provider_id
        self.save(data)

    def get_active(self) -> dict | None:
        """返回当前 active provider 的 record；无 active 时返回 None。"""
        data = self.load()
        active = data["activeProvider"]
        if active is None:
            return None
        return data["providers"][active]
