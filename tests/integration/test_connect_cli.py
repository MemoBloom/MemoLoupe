"""``memoloupe connect`` CLI 集成测试（connect-first 计划 Task 3）。

全部通过 ``run_connect(..., store=..., secrets=...)`` 注入 tmp_path 下的
``ConnectionStore`` 与 ``MemorySecretStore``；health check 的 HTTP 层用
monkeypatch 拦截 ``memoloupe.cli.connect._http_get_status``，绝不发起真实网络请求。
"""

from __future__ import annotations

import pytest

from memoloupe.cli.main import (
    EXIT_INPUT,
    EXIT_OK,
    EXIT_STAGE_FAILED,
    EXIT_USAGE,
    main,
)
from memoloupe.connect.secrets import MemorySecretStore
from memoloupe.connect.store import ConnectionStore

FAKE_KEY = "sk-fake-test-key-0000000001"


@pytest.fixture
def store(tmp_path) -> ConnectionStore:
    return ConnectionStore(tmp_path / "connections.json")


@pytest.fixture
def secrets() -> MemorySecretStore:
    return MemorySecretStore()


@pytest.fixture
def health_ok(monkeypatch):
    import memoloupe.cli.connect as connect_cli

    monkeypatch.setattr(
        connect_cli, "_http_get_status", lambda url, *, api_key, timeout_sec: 200
    )


@pytest.fixture
def health_fail(monkeypatch):
    import memoloupe.cli.connect as connect_cli

    monkeypatch.setattr(
        connect_cli, "_http_get_status", lambda url, *, api_key, timeout_sec: 401
    )


def _add_qwen(store, secrets) -> int:
    from memoloupe.cli.connect import run_connect

    return run_connect(
        ["add", "qwen", "--api-key-env", "TEST_CONNECT_KEY"],
        store=store,
        secrets=secrets,
    )


class TestConnectAdd:
    def test_add_with_api_key_env(
        self, store, secrets, health_ok, monkeypatch, capsys
    ):
        monkeypatch.setenv("TEST_CONNECT_KEY", FAKE_KEY)
        code = _add_qwen(store, secrets)
        assert code == EXIT_OK

        data = store.load()
        assert "qwen" in data["providers"]
        assert data["activeProvider"] == "qwen"
        record = data["providers"]["qwen"]
        assert record["baseUrl"]
        assert record["models"]["media"]
        assert record["models"]["text"]

        # 凭据进 SecretStore，绝不落 connections.json。
        assert secrets.get("qwen") == FAKE_KEY
        assert FAKE_KEY not in store.path.read_text(encoding="utf-8")

        # health check 通过后给出下一步命令提示。
        out = capsys.readouterr().out
        assert "memoloupe shot" in out

    def test_add_health_failure_saves_config_but_not_active(
        self, store, secrets, health_fail, monkeypatch, capsys
    ):
        monkeypatch.setenv("TEST_CONNECT_KEY", FAKE_KEY)
        code = _add_qwen(store, secrets)
        assert code == EXIT_OK  # health check 失败只是 warning
        data = store.load()
        assert "qwen" in data["providers"]
        assert data["activeProvider"] is None
        assert secrets.get("qwen") == FAKE_KEY
        assert "warning" in capsys.readouterr().err

    def test_add_missing_env_var(self, store, secrets, capsys):
        code = _add_qwen(store, secrets)
        assert code == EXIT_USAGE
        assert "TEST_CONNECT_KEY" in capsys.readouterr().err
        assert store.load()["providers"] == {}

    def test_add_non_interactive_without_api_key_env(self, store, secrets, capsys):
        # pytest 捕获下 stdin 非 TTY：不得挂起等输入，直接按缺 key 报错。
        from memoloupe.cli.connect import run_connect

        code = run_connect(["add", "qwen"], store=store, secrets=secrets)
        assert code == EXIT_USAGE
        assert "--api-key-env" in capsys.readouterr().err
        assert store.load()["providers"] == {}

    def test_add_unknown_provider(self, store, secrets, capsys):
        from memoloupe.cli.connect import run_connect

        code = run_connect(
            ["add", "foo", "--api-key-env", "TEST_CONNECT_KEY"],
            store=store,
            secrets=secrets,
        )
        assert code == EXIT_USAGE
        assert "foo" in capsys.readouterr().err


class TestConnectStatus:
    def test_status_without_provider_shows_onboarding(self, store, secrets, capsys):
        from memoloupe.cli.connect import run_connect

        code = run_connect(["status"], store=store, secrets=secrets)
        assert code == EXIT_OK
        assert "connect add qwen" in capsys.readouterr().out

    def test_status_with_provider(
        self, store, secrets, health_ok, monkeypatch, capsys
    ):
        monkeypatch.setenv("TEST_CONNECT_KEY", FAKE_KEY)
        assert _add_qwen(store, secrets) == EXIT_OK
        capsys.readouterr()  # 清空 add 的输出

        from memoloupe.cli.connect import run_connect

        code = run_connect(["status"], store=store, secrets=secrets)
        assert code == EXIT_OK
        out = capsys.readouterr().out
        assert "qwen" in out
        assert "baseUrl" in out
        assert "models" in out
        assert "secret: 已保存" in out
        assert FAKE_KEY not in out


class TestConnectTest:
    def test_test_success(self, store, secrets, health_ok, monkeypatch):
        monkeypatch.setenv("TEST_CONNECT_KEY", FAKE_KEY)
        assert _add_qwen(store, secrets) == EXIT_OK

        from memoloupe.cli.connect import run_connect

        assert run_connect(["test"], store=store, secrets=secrets) == EXIT_OK

    def test_test_health_failure(self, store, secrets, health_ok, monkeypatch):
        monkeypatch.setenv("TEST_CONNECT_KEY", FAKE_KEY)
        assert _add_qwen(store, secrets) == EXIT_OK

        import memoloupe.cli.connect as connect_cli

        monkeypatch.setattr(
            connect_cli, "_http_get_status", lambda url, *, api_key, timeout_sec: 401
        )
        assert (
            connect_cli.run_connect(["test"], store=store, secrets=secrets)
            == EXIT_STAGE_FAILED
        )

    def test_test_without_provider(self, store, secrets, capsys):
        from memoloupe.cli.connect import run_connect

        code = run_connect(["test"], store=store, secrets=secrets)
        assert code == EXIT_INPUT
        assert "connect add" in capsys.readouterr().err


class TestConnectSwitchRemoveList:
    def test_switch(self, store, secrets, health_ok, monkeypatch, capsys):
        monkeypatch.setenv("TEST_CONNECT_KEY", FAKE_KEY)
        assert _add_qwen(store, secrets) == EXIT_OK
        assert store.load()["activeProvider"] == "qwen"

        import memoloupe.cli.connect as connect_cli

        # mimo health check 失败：配置保留但不成为 active。
        monkeypatch.setattr(
            connect_cli, "_http_get_status", lambda url, *, api_key, timeout_sec: 401
        )
        assert (
            connect_cli.run_connect(
                ["add", "mimo", "--api-key-env", "TEST_CONNECT_KEY"],
                store=store,
                secrets=secrets,
            )
            == EXIT_OK
        )
        assert store.load()["activeProvider"] == "qwen"

        code = connect_cli.run_connect(["switch", "mimo"], store=store, secrets=secrets)
        assert code == EXIT_OK
        assert store.load()["activeProvider"] == "mimo"

    def test_switch_unknown_provider(self, store, secrets, capsys):
        from memoloupe.cli.connect import run_connect

        code = run_connect(["switch", "qwen"], store=store, secrets=secrets)
        assert code == EXIT_INPUT
        assert "qwen" in capsys.readouterr().err

    def test_remove_deletes_record_and_secret(
        self, store, secrets, health_ok, monkeypatch
    ):
        monkeypatch.setenv("TEST_CONNECT_KEY", FAKE_KEY)
        assert _add_qwen(store, secrets) == EXIT_OK
        assert secrets.get("qwen") == FAKE_KEY

        from memoloupe.cli.connect import run_connect

        code = run_connect(["remove", "qwen"], store=store, secrets=secrets)
        assert code == EXIT_OK
        data = store.load()
        assert "qwen" not in data["providers"]
        assert data["activeProvider"] is None
        assert secrets.get("qwen") is None

    def test_remove_unknown_provider(self, store, secrets, capsys):
        from memoloupe.cli.connect import run_connect

        code = run_connect(["remove", "qwen"], store=store, secrets=secrets)
        assert code == EXIT_INPUT

    def test_list(self, store, secrets, health_ok, monkeypatch, capsys):
        monkeypatch.setenv("TEST_CONNECT_KEY", FAKE_KEY)
        assert _add_qwen(store, secrets) == EXIT_OK
        capsys.readouterr()

        from memoloupe.cli.connect import run_connect

        code = run_connect(["list"], store=store, secrets=secrets)
        assert code == EXIT_OK
        out = capsys.readouterr().out
        assert "qwen" in out
        assert "mimo" in out  # 支持的 provider 全部列出
        assert FAKE_KEY not in out


class TestConnectDispatch:
    def test_main_dispatches_connect_prefix(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv(
            "MEMOLOUPE_CONNECTIONS_PATH", str(tmp_path / "connections.json")
        )
        monkeypatch.setenv("MEMOLOUPE_SECRET_STORE", "memory")
        code = main(["connect", "status"])
        assert code == EXIT_OK
        assert "connect add qwen" in capsys.readouterr().out

    def test_connect_help_shows_subcommands(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            main(["connect", "--help"])
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        for sub in ("add", "status", "test", "switch", "remove", "list"):
            assert sub in out
