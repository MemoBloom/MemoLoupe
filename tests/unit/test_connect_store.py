"""connect.store 单元测试：连接存储的加载、校验与原子写。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from memoloupe.connect.store import (
    ConnectionStore,
    ConnectionStoreError,
    default_connections_path,
)


def _record(provider_id: str = "qwen") -> dict:
    return {
        "providerId": provider_id,
        "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": {"media": "qwen3.5-omni", "text": "qwen-plus", "asr": None},
        "capabilities": {"mediaUnderstanding": True, "text": True, "asr": False},
    }


class TestDefaultPath:
    def test_env_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        custom = tmp_path / "custom-connections.json"
        monkeypatch.setenv("MEMOLOUPE_CONNECTIONS_PATH", str(custom))
        assert default_connections_path() == custom

    def test_default_under_config_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MEMOLOUPE_CONNECTIONS_PATH", raising=False)
        path = default_connections_path()
        assert path.name == "connections.json"
        assert path.parent.name == "memoloupe"


class TestLoad:
    def test_missing_file_returns_skeleton(self, tmp_path: Path) -> None:
        store = ConnectionStore(tmp_path / "connections.json")
        assert store.load() == {
            "version": 1,
            "activeProvider": None,
            "providers": {},
        }

    def test_bad_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "connections.json"
        path.write_text("{not json", encoding="utf-8")
        store = ConnectionStore(path)
        with pytest.raises(ConnectionStoreError):
            store.load()

    def test_version_2_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "connections.json"
        path.write_text(
            json.dumps({"version": 2, "activeProvider": None, "providers": {}}),
            encoding="utf-8",
        )
        store = ConnectionStore(path)
        with pytest.raises(ConnectionStoreError):
            store.load()

    def test_unknown_provider_id_raises(self, tmp_path: Path) -> None:
        store = ConnectionStore(tmp_path / "connections.json")
        with pytest.raises(ConnectionStoreError):
            store.upsert_provider(_record("openai-compatible"), make_active=False)

    def test_dangling_active_provider_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "connections.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "activeProvider": "qwen",
                    "providers": {},
                }
            ),
            encoding="utf-8",
        )
        store = ConnectionStore(path)
        with pytest.raises(ConnectionStoreError):
            store.load()

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        store = ConnectionStore(tmp_path / "connections.json")
        record = _record()
        del record["baseUrl"]
        with pytest.raises(ConnectionStoreError):
            store.upsert_provider(record, make_active=False)

    def test_models_missing_media_raises(self, tmp_path: Path) -> None:
        store = ConnectionStore(tmp_path / "connections.json")
        record = _record()
        record["models"] = {"text": "qwen-plus"}
        with pytest.raises(ConnectionStoreError):
            store.upsert_provider(record, make_active=False)


class TestUpsert:
    def test_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "connections.json"
        store = ConnectionStore(path)
        store.upsert_provider(_record(), make_active=False)
        data = ConnectionStore(path).load()
        assert data["providers"]["qwen"] == _record()
        assert data["activeProvider"] is None

    def test_make_active_updates_active_provider(self, tmp_path: Path) -> None:
        path = tmp_path / "connections.json"
        store = ConnectionStore(path)
        store.upsert_provider(_record(), make_active=True)
        data = ConnectionStore(path).load()
        assert data["activeProvider"] == "qwen"

    def test_api_key_rejected(self, tmp_path: Path) -> None:
        store = ConnectionStore(tmp_path / "connections.json")
        record = {**_record(), "apiKey": "sk-secret"}
        with pytest.raises(ConnectionStoreError):
            store.upsert_provider(record, make_active=False)

    def test_saved_file_has_no_api_key(self, tmp_path: Path) -> None:
        path = tmp_path / "connections.json"
        store = ConnectionStore(path)
        store.upsert_provider(_record(), make_active=True)
        assert "apiKey" not in path.read_text(encoding="utf-8")

    def test_file_permissions_0600(self, tmp_path: Path) -> None:
        path = tmp_path / "connections.json"
        store = ConnectionStore(path)
        store.upsert_provider(_record(), make_active=False)
        mode = os.stat(path).st_mode & 0o777
        assert mode == 0o600


class TestRemoveAndActive:
    def test_remove_provider(self, tmp_path: Path) -> None:
        path = tmp_path / "connections.json"
        store = ConnectionStore(path)
        store.upsert_provider(_record(), make_active=False)
        store.remove_provider("qwen")
        data = ConnectionStore(path).load()
        assert data["providers"] == {}

    def test_remove_clears_active_provider(self, tmp_path: Path) -> None:
        path = tmp_path / "connections.json"
        store = ConnectionStore(path)
        store.upsert_provider(_record(), make_active=True)
        store.remove_provider("qwen")
        data = ConnectionStore(path).load()
        assert data["activeProvider"] is None

    def test_remove_unknown_raises(self, tmp_path: Path) -> None:
        store = ConnectionStore(tmp_path / "connections.json")
        with pytest.raises(ConnectionStoreError):
            store.remove_provider("qwen")

    def test_set_active(self, tmp_path: Path) -> None:
        path = tmp_path / "connections.json"
        store = ConnectionStore(path)
        store.upsert_provider(_record(), make_active=False)
        store.set_active("qwen")
        assert ConnectionStore(path).load()["activeProvider"] == "qwen"

    def test_set_active_unknown_raises(self, tmp_path: Path) -> None:
        store = ConnectionStore(tmp_path / "connections.json")
        with pytest.raises(ConnectionStoreError):
            store.set_active("qwen")

    def test_get_active(self, tmp_path: Path) -> None:
        path = tmp_path / "connections.json"
        store = ConnectionStore(path)
        assert store.get_active() is None
        store.upsert_provider(_record(), make_active=True)
        active = ConnectionStore(path).get_active()
        assert active is not None
        assert active["providerId"] == "qwen"
