"""connect.runtime 单元测试：active provider 叠加到管道配置（connect-first Task 4）。"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from memoloupe.connect.runtime import resolve_active_provider
from memoloupe.connect.secrets import MemorySecretStore
from memoloupe.connect.store import ConnectionStore, ConnectionStoreError
from memoloupe.core.config import DEFAULT_CONFIG

FAKE_KEY = "sk-fake-runtime-key-0001"
BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _record(
    provider_id: str = "qwen",
    *,
    asr_model: str | None = None,
    asr_capability: bool = False,
) -> dict:
    return {
        "providerId": provider_id,
        "baseUrl": BASE_URL,
        "models": {"media": "qwen3.5-omni", "text": "qwen-plus", "asr": asr_model},
        "capabilities": {
            "mediaUnderstanding": True,
            "text": True,
            "asr": asr_capability,
        },
    }


def _store(tmp_path: Path, record: dict | None = None) -> ConnectionStore:
    store = ConnectionStore(tmp_path / "connections.json")
    if record is not None:
        store.upsert_provider(record, make_active=True)
    return store


def _secrets(with_key: bool = True) -> MemorySecretStore:
    secrets = MemorySecretStore()
    if with_key:
        secrets.set("qwen", FAKE_KEY)
    return secrets


def _config() -> dict:
    return copy.deepcopy(DEFAULT_CONFIG)


class TestProviderOverlay:
    def test_overlays_unified_and_text_model(self, tmp_path: Path) -> None:
        config = _config()
        store = _store(tmp_path, _record())
        resolved, source = resolve_active_provider(
            config, store=store, secrets=_secrets()
        )
        assert source == "provider"
        for group, model in (
            ("unifiedModel", "qwen3.5-omni"),
            ("textModel", "qwen-plus"),
        ):
            assert resolved[group]["baseUrl"] == BASE_URL
            assert resolved[group]["apiKey"] == FAKE_KEY
            assert resolved[group]["model"] == model

    def test_overlay_produces_new_dict_without_mutating_input(
        self, tmp_path: Path
    ) -> None:
        config = _config()
        snapshot = copy.deepcopy(config)
        store = _store(tmp_path, _record())
        resolved, _ = resolve_active_provider(config, store=store, secrets=_secrets())
        assert resolved is not config
        assert resolved["unifiedModel"] is not config["unifiedModel"]
        assert config == snapshot, "入参 config 不得被修改"

    def test_other_groups_and_keys_unchanged(self, tmp_path: Path) -> None:
        config = _config()
        config["unifiedModel"]["timeoutSec"] = 42.0
        store = _store(tmp_path, _record())
        resolved, _ = resolve_active_provider(config, store=store, secrets=_secrets())
        # 同组其余键保留
        assert resolved["unifiedModel"]["timeoutSec"] == 42.0
        assert resolved["unifiedModel"]["fallbackModel"] is None
        # 其余分组不变
        assert resolved["shots"] == config["shots"]
        assert resolved["ffmpeg"] == config["ffmpeg"]
        assert resolved["story"] == config["story"]

    def test_provider_without_asr_capability_keeps_asr_group(
        self, tmp_path: Path
    ) -> None:
        config = _config()
        store = _store(tmp_path, _record(asr_capability=False))
        resolved, _ = resolve_active_provider(config, store=store, secrets=_secrets())
        assert resolved["asr"] == config["asr"]

    def test_provider_with_asr_overlays_asr_group_but_not_transport(
        self, tmp_path: Path
    ) -> None:
        config = _config()
        store = _store(
            tmp_path, _record(asr_model="qwen3-asr-flash", asr_capability=True)
        )
        resolved, source = resolve_active_provider(
            config, store=store, secrets=_secrets()
        )
        assert source == "provider"
        assert resolved["asr"]["baseUrl"] == BASE_URL
        assert resolved["asr"]["apiKey"] == FAKE_KEY
        assert resolved["asr"]["model"] == "qwen3-asr-flash"
        # record 未声明 asrTransport：asr.provider 保留原值
        assert resolved["asr"]["provider"] == config["asr"]["provider"]
        assert resolved["asr"]["vad"] == config["asr"]["vad"]

    def test_provider_with_asr_transport_overlays_provider(
        self, tmp_path: Path
    ) -> None:
        # record 声明 asrTransport（如 mimo-chat）→ asr.provider 同步覆盖（D-057）。
        config = _config()
        record = _record(asr_model="mimo-v2.5-asr", asr_capability=True)
        record["asrTransport"] = "mimo-chat"
        store = _store(tmp_path, record)
        resolved, _ = resolve_active_provider(config, store=store, secrets=_secrets())
        assert resolved["asr"]["provider"] == "mimo-chat"
        assert resolved["asr"]["model"] == "mimo-v2.5-asr"

    def test_missing_secret_raises_with_connect_hint(self, tmp_path: Path) -> None:
        store = _store(tmp_path, _record())
        with pytest.raises(ConnectionStoreError) as excinfo:
            resolve_active_provider(
                _config(), store=store, secrets=_secrets(with_key=False)
            )
        message = str(excinfo.value)
        assert "connect test" in message
        assert "connect add" in message
        assert FAKE_KEY not in message


class TestNoProvider:
    def test_env_config_complete_returns_env(self, tmp_path: Path) -> None:
        config = _config()
        config["unifiedModel"].update(
            {"baseUrl": "https://example.com/v1", "apiKey": "sk-env", "model": "m"}
        )
        resolved, source = resolve_active_provider(
            config, store=_store(tmp_path), secrets=_secrets(with_key=False)
        )
        assert source == "env"
        assert resolved == config

    def test_env_text_model_only_also_counts_as_env(self, tmp_path: Path) -> None:
        config = _config()
        config["textModel"].update(
            {"baseUrl": "https://example.com/v1", "apiKey": "sk-env", "model": "m"}
        )
        _, source = resolve_active_provider(
            config, store=_store(tmp_path), secrets=_secrets(with_key=False)
        )
        assert source == "env"

    def test_nothing_configured_returns_none(self, tmp_path: Path) -> None:
        config = _config()
        resolved, source = resolve_active_provider(
            config, store=_store(tmp_path), secrets=_secrets(with_key=False)
        )
        assert source == "none"
        assert resolved == config

    def test_partial_env_config_is_not_env(self, tmp_path: Path) -> None:
        config = _config()
        config["unifiedModel"].update({"baseUrl": "https://example.com/v1"})
        _, source = resolve_active_provider(
            config, store=_store(tmp_path), secrets=_secrets(with_key=False)
        )
        assert source == "none"
