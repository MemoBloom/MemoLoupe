"""connect.secrets 单元测试：凭据存储与脱敏。"""

from __future__ import annotations

import subprocess

import pytest

from memoloupe.connect.secrets import (
    KeychainSecretStore,
    MemorySecretStore,
    default_secret_store,
    redact_secret,
)


class TestMemorySecretStore:
    def test_set_get_roundtrip(self) -> None:
        store = MemorySecretStore()
        store.set("qwen", "sk-123")
        assert store.get("qwen") == "sk-123"

    def test_get_missing_returns_none(self) -> None:
        assert MemorySecretStore().get("qwen") is None

    def test_delete(self) -> None:
        store = MemorySecretStore()
        store.set("qwen", "sk-123")
        store.delete("qwen")
        assert store.get("qwen") is None

    def test_delete_missing_is_noop(self) -> None:
        MemorySecretStore().delete("qwen")


class TestKeychainSecretStore:
    def _patch_run(
        self, monkeypatch: pytest.MonkeyPatch, returncode: int = 0, stdout: str = ""
    ) -> list[list[str]]:
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):  # noqa: ANN001, ANN202
            calls.append(list(argv))
            return subprocess.CompletedProcess(
                argv, returncode=returncode, stdout=stdout, stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        return calls

    def test_set_uses_security_add(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._patch_run(monkeypatch)
        store = KeychainSecretStore()
        store.set("qwen", "sk-123")
        assert len(calls) == 1
        argv = calls[0]
        assert argv[0] == "/usr/bin/security"
        assert "add-generic-password" in argv
        assert "memoloupe" in argv
        assert "provider:qwen" in argv
        assert "sk-123" in argv

    def test_get_returns_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._patch_run(monkeypatch, returncode=0, stdout="sk-123\n")
        store = KeychainSecretStore()
        assert store.get("qwen") == "sk-123"
        argv = calls[0]
        assert "find-generic-password" in argv
        assert "memoloupe" in argv
        assert "provider:qwen" in argv

    def test_get_nonzero_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_run(monkeypatch, returncode=44)
        assert KeychainSecretStore().get("qwen") is None

    def test_delete_missing_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._patch_run(monkeypatch, returncode=44)
        KeychainSecretStore().delete("qwen")
        argv = calls[0]
        assert "delete-generic-password" in argv
        assert "provider:qwen" in argv


class TestRedactSecret:
    def test_secret_removed(self) -> None:
        result = redact_secret("key is sk-123", "sk-123")
        assert "sk-123" not in result

    def test_none_secret_returns_text(self) -> None:
        assert redact_secret("hello", None) == "hello"


class TestDefaultSecretStore:
    def test_env_forces_memory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEMOLOUPE_SECRET_STORE", "memory")
        assert isinstance(default_secret_store(), MemorySecretStore)
