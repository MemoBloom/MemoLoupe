"""connect-first Task 4 集成测试：管道 CLI 经 active provider 解析模型配置。

全程不发起网络请求、不读写真实用户目录：

- connections 路径用 ``MEMOLOUPE_CONNECTIONS_PATH`` 指到 tmp_path；
- SecretStore 通过 monkeypatch ``connect.runtime.default_secret_store``
  换成共享的 MemorySecretStore；
- 文本模型构造与故事管线/渲染全部拦截，只验证"配置如何到达服务构造层"。
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

import memoloupe.cli.story_analysis as story_cli
import memoloupe.connect.runtime as runtime_mod
from memoloupe.cli.main import EXIT_OK, EXIT_USAGE
from memoloupe.connect.runtime import resolve_active_provider  # noqa: F401
from memoloupe.connect.secrets import MemorySecretStore
from memoloupe.connect.store import ConnectionStore
from memoloupe.core.config import DEFAULT_CONFIG

FAKE_KEY = "sk-fake-routing-key-0001"
BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _record() -> dict:
    return {
        "providerId": "qwen",
        "baseUrl": BASE_URL,
        "models": {"media": "qwen3.5-omni", "text": "qwen-plus", "asr": None},
        "capabilities": {"mediaUnderstanding": True, "text": True, "asr": False},
    }


@pytest.fixture
def connections_path(tmp_path: Path, monkeypatch) -> Path:
    """把 connections 路径指到 tmp_path，隔离真实用户目录。"""
    path = tmp_path / "connections.json"
    monkeypatch.setenv("MEMOLOUPE_CONNECTIONS_PATH", str(path))
    return path


@pytest.fixture
def secrets(monkeypatch) -> MemorySecretStore:
    """共享的内存凭据存储（runtime 惰性调用 default_secret_store）。"""
    store = MemorySecretStore()
    monkeypatch.setattr(runtime_mod, "default_secret_store", lambda: store)
    return store


@pytest.fixture
def captured_story(monkeypatch, tmp_path: Path) -> dict:
    """拦截 story 管线：捕获传入文本模型构造层的 config，短路执行与渲染。"""
    from memoloupe.analysis.shot_pipeline import PipelineReport

    captured: dict = {}

    def fake_build_text_service(config):
        captured["config"] = config
        return None, "captured"

    def fake_run(self, request):
        captured["request"] = request
        return PipelineReport(
            phase="story",
            status="complete",
            steps=[],
            warnings=[],
            artifacts=[],
            elapsed_ms=0,
        )

    monkeypatch.setattr(story_cli, "build_text_model_service", fake_build_text_service)
    monkeypatch.setattr(story_cli.StoryAnalysisPipeline, "run", fake_run)
    monkeypatch.setattr(story_cli, "render_story_html", lambda out_dir: None)
    return captured


def _run_story(out_dir: Path) -> int:
    return story_cli.run_story_analysis(
        ["--output-dir", str(out_dir), "--allow-draft"]
    )


class TestStoryCliProviderRouting:
    def test_active_provider_reaches_text_model_config(
        self, connections_path, secrets, captured_story, tmp_path, capsys
    ) -> None:
        store = ConnectionStore(connections_path)
        store.upsert_provider(_record(), make_active=True)
        secrets.set("qwen", FAKE_KEY)

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        code = _run_story(out_dir)

        assert code == EXIT_OK
        text_cfg = captured_story["config"]["textModel"]
        assert text_cfg["baseUrl"] == BASE_URL
        assert text_cfg["apiKey"] == FAKE_KEY
        assert text_cfg["model"] == "qwen-plus"
        assert "connect status" in capsys.readouterr().err

    def test_missing_secret_is_explicit_usage_error(
        self, connections_path, secrets, captured_story, tmp_path, capsys
    ) -> None:
        store = ConnectionStore(connections_path)
        store.upsert_provider(_record(), make_active=True)
        # 故意不保存凭据。

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        code = _run_story(out_dir)

        assert code == EXIT_USAGE
        err = capsys.readouterr().err
        assert "connect test" in err and "connect add" in err
        assert FAKE_KEY not in err
        assert "config" not in captured_story  # 未进入服务构造层

    def test_env_fallback_when_no_provider(
        self, connections_path, secrets, captured_story, tmp_path, monkeypatch, capsys
    ) -> None:
        monkeypatch.setenv("MEMOLOUPE_TEXTMODEL__BASEURL", "https://env.example/v1")
        monkeypatch.setenv("MEMOLOUPE_TEXTMODEL__APIKEY", "sk-env-key")
        monkeypatch.setenv("MEMOLOUPE_TEXTMODEL__MODEL", "env-model")

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        code = _run_story(out_dir)

        assert code == EXIT_OK
        text_cfg = captured_story["config"]["textModel"]
        assert text_cfg["baseUrl"] == "https://env.example/v1"
        assert text_cfg["apiKey"] == "sk-env-key"
        assert text_cfg["model"] == "env-model"
        assert "connect status" not in capsys.readouterr().err

    def test_nothing_configured_shows_connect_hint(
        self, connections_path, secrets, tmp_path, monkeypatch, capsys
    ) -> None:
        from memoloupe.analysis.shot_pipeline import PipelineReport

        monkeypatch.setattr(story_cli, "load_config", lambda: copy.deepcopy(DEFAULT_CONFIG))
        monkeypatch.setattr(
            story_cli.StoryAnalysisPipeline,
            "run",
            lambda self, request: PipelineReport(
                phase="story",
                status="complete",
                steps=[],
                warnings=[],
                artifacts=[],
                elapsed_ms=0,
            ),
        )
        monkeypatch.setattr(story_cli, "render_story_html", lambda out_dir: None)

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        code = _run_story(out_dir)

        assert code == EXIT_OK
        assert "connect add qwen" in capsys.readouterr().err
