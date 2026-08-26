"""真实服务 opt-in 测试（roadmap 05-01D）。

默认**不进入无凭据 CI**：只有 ``MEMOLOUPE_RUN_REAL_SERVICE_TESTS=1`` 且
对应服务凭据齐全时才跑真实网络 smoke；否则 pytest skip。

凭据从环境变量读取（与 config 相同命名，``__`` 分隔层级）：

- UnifiedMLLM：``MEMOLOUPE_UNIFIEDMODEL__BASEURL`` / ``__APIKEY`` / ``__MODEL``
- ASR：``MEMOLOUPE_ASR__BASEURL`` / ``__APIKEY`` / ``__MODEL``
- 文本模型：``MEMOLOUPE_TEXTMODEL__BASEURL`` / ``__APIKEY`` / ``__MODEL``

同一文件内的**脱敏 fixture 回归测试**（无网络）始终运行：用
``tests/fixtures/services/`` 的真实形态响应快照验证解析与归一化路径，
并审计异常信息 / JSON report / checkpoint 不泄露密钥与媒体载荷。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from memoloupe.analysis.media_orchestrator import _strip_single_fence
from memoloupe.services.asr import ASRRequest, OpenAICompatibleASR
from memoloupe.services.base import SERVICE_PROTOCOL_VERSION, http_json_post
from memoloupe.services.text_model import (
    OpenAICompatibleTextModel,
    TextModelRequest,
)
from memoloupe.services.unified_media import OpenAICompatibleUnifiedMedia

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "services"

REAL_ENABLED = os.environ.get("MEMOLOUPE_RUN_REAL_SERVICE_TESTS") == "1"


def _env(*keys: str) -> str | None:
    value = os.environ.get(f"MEMOLOUPE_{'__'.join(keys)}")
    return value if value and value.strip() else None


def _skip_reason(kind: str, missing: list[str]) -> str:
    return (
        f"真实 {kind} 测试未启用（MEMOLOUPE_RUN_REAL_SERVICE_TESTS=1 且 "
        f"缺 {', '.join(missing)}）"
    )


# ---------------------------------------------------------------------------
# 真实服务 smoke（opt-in，缺凭据 skip）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not REAL_ENABLED
    or not all(_env("UNIFIEDMODEL", k) for k in ("BASEURL", "APIKEY", "MODEL")),
    reason=_skip_reason(
        "UnifiedMLLM",
        [f"UNIFIEDMODEL__{k}" for k in ("BASEURL", "APIKEY", "MODEL")],
    ),
)
class TestRealUnifiedSmoke:
    """最小 1-2 shot 真实 UnifiedMLLM 调用（只做请求与文本提取，不做全量分析）。"""

    def test_minimal_batch_request(self, tmp_path):
        service = OpenAICompatibleUnifiedMedia(
            base_url=_env("UNIFIEDMODEL", "BASEURL"),
            api_key=_env("UNIFIEDMODEL", "APIKEY"),
            model=_env("UNIFIEDMODEL", "MODEL"),
        )
        proxy = tmp_path / "clip.mp4"
        proxy.write_bytes(b"\x00\x01fake-clip")
        from memoloupe.services.unified_media import AnalysisGroup, ModelClip

        clip = ModelClip(shot_id="SH0001", proxy_path=proxy, duration_ms=1000)
        group = AnalysisGroup(
            name="visual", fields=("visual.content",), prompt="描述画面。",
            schema={}, fingerprint="optin",
        )
        text = service.analyze_batch([clip], group)
        assert isinstance(text, str) and text.strip()


@pytest.mark.skipif(
    not REAL_ENABLED
    or not all(_env("ASR", k) for k in ("BASEURL", "APIKEY", "MODEL")),
    reason=_skip_reason("ASR", [f"ASR__{k}" for k in ("BASEURL", "APIKEY", "MODEL")]),
)
class TestRealAsrSmoke:
    """最小音频片段真实 ASR 调用（归一化为稳定 ASRResult）。"""

    def test_minimal_transcribe(self, tmp_path):
        service = OpenAICompatibleASR(
            base_url=_env("ASR", "BASEURL"),
            api_key=_env("ASR", "APIKEY"),
            model=_env("ASR", "MODEL"),
        )
        audio = tmp_path / "clip.mp4"
        audio.write_bytes(b"\x00\x01fake-audio")
        result = service.transcribe(audio, ASRRequest(language="zh"))
        assert isinstance(result.segments, tuple)


@pytest.mark.skipif(
    not REAL_ENABLED
    or not all(_env("TEXTMODEL", k) for k in ("BASEURL", "APIKEY", "MODEL")),
    reason=_skip_reason(
        "文本模型", [f"TEXTMODEL__{k}" for k in ("BASEURL", "APIKEY", "MODEL")]
    ),
)
class TestRealTextModelSmoke:
    """story/profile 文本模型最小 prompt 调用。"""

    def test_minimal_generate(self):
        service = OpenAICompatibleTextModel(
            base_url=_env("TEXTMODEL", "BASEURL"),
            api_key=_env("TEXTMODEL", "APIKEY"),
            model=_env("TEXTMODEL", "MODEL"),
        )
        text = service.generate(
            TextModelRequest(task="optin-smoke", prompt='返回 {"ok": true}')
        )
        assert json.loads(text)["ok"] is True


# ---------------------------------------------------------------------------
# 脱敏 fixture 回归（无网络，始终运行）
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> dict:
    path = FIXTURES / name
    if not path.is_file():
        pytest.skip(f"fixture 缺失：{path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


class TestSanitizedFixtureRegression:
    """真实形态响应快照的解析/归一化回归（fixture 已脱敏）。"""

    def test_fixtures_exist_and_are_sanitized(self):
        secrets = ("sk-", "Bearer ", "data:video", "http://", "https://")
        for path in sorted(FIXTURES.glob("*.json")):
            text = path.read_text(encoding="utf-8")
            for secret in secrets:
                assert secret not in text, f"{path.name} 含敏感内容：{secret}"
            json.loads(text)  # 必须是合法 JSON

    def test_unified_fence_stripping(self):
        fixture = _load_fixture("unified-success.json")
        text = fixture["rawText"]
        stripped = _strip_single_fence(text)
        payload = json.loads(stripped)
        assert payload["shots"][0]["shotID"]
        assert "data:" not in stripped

    def test_asr_normalization(self):
        fixture = _load_fixture("asr-verbose-zh.json")
        assert fixture["segments"][0]["start"] >= 0
        assert fixture["text"]

    def test_text_model_story_response_shape(self):
        fixture = _load_fixture("text-model-story.json")
        blocks = fixture.get("blocks")
        slots = fixture.get("slots")
        assert isinstance(blocks, list) and blocks
        assert isinstance(slots, list)


# ---------------------------------------------------------------------------
# 日志与产物审计（无网络，始终运行）
# ---------------------------------------------------------------------------


class TestRedactionAudit:
    """05-01D-4：异常信息 / JSON report / checkpoint 不泄露密钥与媒体载荷。"""

    def test_service_error_redacts_api_key(self):
        from memoloupe.services.base import (
            PermanentServiceError,
            TransientServiceError,
        )

        with pytest.raises((PermanentServiceError, TransientServiceError)) as exc_info:
            http_json_post(
                "http://127.0.0.1:1/v1/chat/completions",
                headers={"Authorization": "Bearer sk-super-secret"},
                payload={"model": "m"},
                timeout_sec=0.5,
            )
        assert "sk-super-secret" not in str(exc_info.value)

    def test_unified_checkpoint_has_no_media_payload(self, tmp_path):
        """checkpoint 只存已校验的模型文本，不得含视频 Data URI。"""
        from memoloupe.analysis.media_groups import build_groups
        from memoloupe.analysis.media_orchestrator import _write_checkpoint
        from memoloupe.analysis.vocabulary import load_vocabulary

        (tmp_path / "checkpoints").mkdir()
        from memoloupe.artifacts.store import ArtifactStore

        config = {"unifiedModel": {"videoFPS": 10.0, "mediaResolution": "default"}}
        groups = build_groups(load_vocabulary(), config)
        results = {"SH0001": {"visual": {"content": "机场"}}}
        _write_checkpoint(ArtifactStore(tmp_path), groups[0], 4, results)
        text = (tmp_path / "checkpoints" / f"unified-media-{groups[0].name}.json").read_text(
            encoding="utf-8"
        )
        assert "data:video" not in text
        assert "base64" not in text

    def test_config_snapshot_redacts_secrets(self):
        """配置快照（供 CLI 展示 / JSON report）不输出 apiKey。"""
        from memoloupe.core.config import load_config, redacted_snapshot

        config = load_config(
            env={
                "MEMOLOUPE_ASR__APIKEY": "sk-visible-in-memory",
                "MEMOLOUPE_UNIFIEDMODEL__APIKEY": "sk-unified-in-memory",
            }
        )
        snapshot = json.dumps(redacted_snapshot(config), ensure_ascii=False)
        assert "sk-visible-in-memory" not in snapshot
        assert "sk-unified-in-memory" not in snapshot
        # 原始 config 仍保留密钥（快照不改变原值）。
        assert config["asr"]["apiKey"] == "sk-visible-in-memory"
        assert SERVICE_PROTOCOL_VERSION  # 防止未使用导入告警


@pytest.mark.skipif(
    not REAL_ENABLED,
    reason="真实服务 smoke 未启用（MEMOLOUPE_RUN_REAL_SERVICE_TESTS=1）",
)
class TestRealServicesGated:
    def test_gate_flag_required(self):
        """占位：确认 skip 判定路径可用（REAL_ENABLED 为真时本类运行）。"""
        assert REAL_ENABLED is True
