"""config 模块单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from memoloupe.core.atomic_io import write_json_atomic
from memoloupe.core.config import (
    DEFAULT_CONFIG,
    config_fingerprint,
    deep_merge,
    load_config,
    load_env_file,
    redacted_snapshot,
)
from memoloupe.core.errors import ConfigError


class TestDefaultConfig:
    def test_groups_present(self) -> None:
        for group in (
            "runtime",
            "ffmpeg",
            "shots",
            "audioCuts",
            "music",
            "quality",
            "vision",
            "asr",
            "unifiedModel",
            "textModel",
            "story",
            "profile",
            "render",
        ):
            assert group in DEFAULT_CONFIG

    def test_doc_defaults(self) -> None:
        assert DEFAULT_CONFIG["audioCuts"]["analysisSampleRate"] == 16000
        assert DEFAULT_CONFIG["audioCuts"]["frameMs"] == 20
        assert DEFAULT_CONFIG["audioCuts"]["threshold"] == 8.0
        assert DEFAULT_CONFIG["audioCuts"]["syncToleranceMs"] == 100
        assert DEFAULT_CONFIG["audioCuts"]["associationWindowMs"] == 500
        assert DEFAULT_CONFIG["music"]["silentLevelDb"] == -55.0
        assert DEFAULT_CONFIG["music"]["musicMinimumRunMs"] == 400
        assert DEFAULT_CONFIG["music"]["musicMergeGapMs"] == 600
        assert DEFAULT_CONFIG["story"]["gapMs"] == 1200
        assert DEFAULT_CONFIG["asr"]["baseUrl"] is None
        assert DEFAULT_CONFIG["asr"]["apiKey"] is None
        assert DEFAULT_CONFIG["asr"]["model"] is None
        assert DEFAULT_CONFIG["asr"]["timeoutSec"] == 120.0
        assert DEFAULT_CONFIG["unifiedModel"]["baseUrl"] is None
        assert DEFAULT_CONFIG["unifiedModel"]["apiKey"] is None
        assert DEFAULT_CONFIG["unifiedModel"]["model"] is None
        assert DEFAULT_CONFIG["unifiedModel"]["fallbackModel"] is None
        assert DEFAULT_CONFIG["unifiedModel"]["timeoutSec"] == 300.0
        assert DEFAULT_CONFIG["unifiedModel"]["batchSize"] == 4
        assert DEFAULT_CONFIG["unifiedModel"]["concurrency"] == 10
        assert DEFAULT_CONFIG["unifiedModel"]["videoFPS"] == 10.0
        assert DEFAULT_CONFIG["unifiedModel"]["mediaResolution"] == "default"
        assert DEFAULT_CONFIG["unifiedModel"]["maxRetries"] == 3
        assert DEFAULT_CONFIG["textModel"]["baseUrl"] is None
        assert DEFAULT_CONFIG["textModel"]["apiKey"] is None
        assert DEFAULT_CONFIG["textModel"]["model"] is None
        assert DEFAULT_CONFIG["textModel"]["timeoutSec"] == 300.0
        assert DEFAULT_CONFIG["textModel"]["maxTokens"] == 0
        assert DEFAULT_CONFIG["vision"]["sampleFps"] == 2.0
        assert DEFAULT_CONFIG["vision"]["maximumFramesPerShot"] == 12
        assert DEFAULT_CONFIG["vision"]["maximumImageDimension"] == 960
        assert DEFAULT_CONFIG["quality"]["videoSampleFps"] == 2.0
        assert DEFAULT_CONFIG["quality"]["blurFlagThreshold"] == 11.0
        assert DEFAULT_CONFIG["quality"]["underexposedYAVG"] == 40.0
        assert DEFAULT_CONFIG["quality"]["overexposedYAVG"] == 215.0
        assert DEFAULT_CONFIG["shots"]["histogramBins"] == 254
        assert DEFAULT_CONFIG["shots"]["analysisSize"] == 128
        assert DEFAULT_CONFIG["shots"]["minimumFrames"] == 8
        assert DEFAULT_CONFIG["shots"]["fullFrameRate"] is True
        assert DEFAULT_CONFIG["shots"]["minimumShotMs"] == 500
        assert DEFAULT_CONFIG["shots"]["adaptiveThreshold"] == 3.5


class TestDeepMerge:
    def test_override_scalar(self) -> None:
        base = {"a": 1, "b": {"x": 1, "y": 2}}
        merged = deep_merge(base, {"b": {"x": 9}})
        assert merged == {"a": 1, "b": {"x": 9, "y": 2}}

    def test_does_not_mutate_base(self) -> None:
        base = {"b": {"x": 1}}
        deep_merge(base, {"b": {"x": 2}})
        assert base == {"b": {"x": 1}}

    def test_new_group_added(self) -> None:
        assert deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_non_dict_override_raises(self) -> None:
        with pytest.raises(ConfigError):
            deep_merge({}, [1, 2])


class TestLoadConfig:
    def test_defaults_when_no_sources(self) -> None:
        assert load_config(env={}) == DEFAULT_CONFIG

    def test_priority_cli_over_env_over_file_over_default(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.json"
        write_json_atomic(config_file, {"shots": {"minimumFrames": 10}})
        env = {"MEMOLOUPE_SHOTS__MINIMUMFRAMES": "12"}
        # 文件覆盖默认
        assert load_config(env={}, config_file=config_file)["shots"][
            "minimumFrames"
        ] == 10
        # 环境变量覆盖文件
        assert load_config(env=env, config_file=config_file)["shots"][
            "minimumFrames"
        ] == 12
        # CLI 覆盖一切
        cli = {"shots": {"minimumFrames": 16}}
        assert load_config(cli_overrides=cli, env=env, config_file=config_file)[
            "shots"
        ]["minimumFrames"] == 16

    def test_env_case_insensitive_and_type_coerced(self) -> None:
        env = {"MEMOLOUPE_SHOTS__MINIMUMFRAMES": "6"}
        config = load_config(env=env)
        assert config["shots"]["minimumFrames"] == 6
        assert isinstance(config["shots"]["minimumFrames"], int)

    def test_env_float_and_bool(self) -> None:
        env = {
            "MEMOLOUPE_QUALITY__BLURFLAGTHRESHOLD": "13.5",
            "MEMOLOUPE_MUSIC__ENABLED": "false",
            "MEMOLOUPE_TEXTMODEL__TIMEOUTSEC": "12.5",
            "MEMOLOUPE_TEXTMODEL__MAXTOKENS": "2048",
            "MEMOLOUPE_ASR__TIMEOUTSEC": "30.5",
            "MEMOLOUPE_UNIFIEDMODEL__TIMEOUTSEC": "99.5",
        }
        config = load_config(env=env)
        assert config["quality"]["blurFlagThreshold"] == 13.5
        assert config["music"]["enabled"] is False
        assert config["textModel"]["timeoutSec"] == 12.5
        assert config["textModel"]["maxTokens"] == 2048
        assert config["asr"]["timeoutSec"] == 30.5
        assert config["unifiedModel"]["timeoutSec"] == 99.5

    def test_env_optional_service_strings(self) -> None:
        env = {
            "MEMOLOUPE_TEXTMODEL__BASEURL": "https://api.example.test/v1",
            "MEMOLOUPE_TEXTMODEL__APIKEY": "sk-text",
            "MEMOLOUPE_TEXTMODEL__MODEL": "story-model",
            "MEMOLOUPE_ASR__BASEURL": "https://asr.example.test/v1",
            "MEMOLOUPE_ASR__APIKEY": "sk-asr",
            "MEMOLOUPE_ASR__MODEL": "asr-model",
            "MEMOLOUPE_UNIFIEDMODEL__BASEURL": "https://vision.example.test/v1",
            "MEMOLOUPE_UNIFIEDMODEL__APIKEY": "sk-unified",
            "MEMOLOUPE_UNIFIEDMODEL__MODEL": "unified-model",
            "MEMOLOUPE_UNIFIEDMODEL__FALLBACKMODEL": "unified-fallback",
        }
        config = load_config(env=env)
        assert config["textModel"]["baseUrl"] == "https://api.example.test/v1"
        assert config["textModel"]["apiKey"] == "sk-text"
        assert config["textModel"]["model"] == "story-model"
        assert config["asr"]["baseUrl"] == "https://asr.example.test/v1"
        assert config["asr"]["apiKey"] == "sk-asr"
        assert config["asr"]["model"] == "asr-model"
        assert config["unifiedModel"]["baseUrl"] == "https://vision.example.test/v1"
        assert config["unifiedModel"]["apiKey"] == "sk-unified"
        assert config["unifiedModel"]["model"] == "unified-model"
        assert config["unifiedModel"]["fallbackModel"] == "unified-fallback"

    def test_env_unknown_key_raises(self) -> None:
        with pytest.raises(ConfigError):
            load_config(env={"MEMOLOUPE_SHOTS__NOSUCHKEY": "1"})
        with pytest.raises(ConfigError):
            load_config(env={"MEMOLOUPE_NOSUCHGROUP__X": "1"})

    def test_env_bad_type_raises(self) -> None:
        with pytest.raises(ConfigError):
            load_config(env={"MEMOLOUPE_SHOTS__MINIMUMFRAMES": "abc"})

    def test_non_memoloupe_env_ignored(self) -> None:
        config = load_config(env={"PATH": "/usr/bin", "HOME": "/x"})
        assert config == DEFAULT_CONFIG

    def test_connect_reserved_env_keys_ignored(self) -> None:
        # connect 子系统的进程级变量不属于配置树，不得被当作未知配置项拒绝。
        config = load_config(
            env={
                "MEMOLOUPE_CONNECTIONS_PATH": "/tmp/connections.json",
                "MEMOLOUPE_SECRET_STORE": "memory",
            }
        )
        assert config == DEFAULT_CONFIG


class TestRedactedSnapshot:
    def test_sensitive_keys_masked(self) -> None:
        config = {
            "unifiedModel": {
                "apiKey": "sk-secret",
                "batchSize": 4,
                "nested": {"access_token": "t", "PASSWORD": "p", "clientSecret": "s"},
            }
        }
        snapshot = redacted_snapshot(config)
        assert snapshot["unifiedModel"]["apiKey"] == "***"
        assert snapshot["unifiedModel"]["batchSize"] == 4
        assert snapshot["unifiedModel"]["nested"]["access_token"] == "***"
        assert snapshot["unifiedModel"]["nested"]["PASSWORD"] == "***"
        assert snapshot["unifiedModel"]["nested"]["clientSecret"] == "***"

    def test_case_insensitive(self) -> None:
        assert redacted_snapshot({"APIKEY": "v"})["APIKEY"] == "***"
        assert redacted_snapshot({"Token": "v"})["Token"] == "***"

    def test_does_not_mutate_input(self) -> None:
        config = {"apiKey": "real"}
        redacted_snapshot(config)
        assert config["apiKey"] == "real"


class TestConfigFingerprint:
    def test_secret_value_does_not_change_fingerprint(self) -> None:
        a = load_config(env={})
        b = load_config(cli_overrides={"unifiedModel": {"apiKey": "sk-aaa"}}, env={})
        c = load_config(cli_overrides={"unifiedModel": {"apiKey": "sk-bbb"}}, env={})
        groups = ["unifiedModel", "shots"]
        assert config_fingerprint(a, groups) == config_fingerprint(b, groups)
        assert config_fingerprint(b, groups) == config_fingerprint(c, groups)

    def test_shots_change_changes_fingerprint(self) -> None:
        a = load_config(env={})
        b = load_config(cli_overrides={"shots": {"minimumFrames": 9}}, env={})
        groups = ["shots"]
        assert config_fingerprint(a, groups) != config_fingerprint(b, groups)

    def test_other_group_change_does_not_affect_fingerprint(self) -> None:
        a = load_config(env={})
        b = load_config(cli_overrides={"story": {"gapMs": 999}}, env={})
        assert config_fingerprint(a, ["shots"]) == config_fingerprint(b, ["shots"])


class TestLoadEnvFile:
    def test_parses_key_values_and_ignores_comments(self, tmp_path: Path) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text(
            "# comment\nMEMOLOUPE_ASR__APIKEY=sk-from-env\n\n"
            "MEMOLOUPE_ASR__MODEL='whisper-2'\n",
            encoding="utf-8",
        )
        result = load_env_file(env_path)
        assert result["MEMOLOUPE_ASR__APIKEY"] == "sk-from-env"
        assert result["MEMOLOUPE_ASR__MODEL"] == "whisper-2"

    def test_does_not_override_existing_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("MEMOLOUPE_ASR__APIKEY", "sk-existing")
        env_path = tmp_path / ".env"
        env_path.write_text("MEMOLOUPE_ASR__APIKEY=sk-from-env\n", encoding="utf-8")
        result = load_env_file(env_path)
        assert "MEMOLOUPE_ASR__APIKEY" not in result

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_env_file(tmp_path / "nope.env") == {}

    def test_env_file_feeds_load_config(self, tmp_path: Path, monkeypatch) -> None:
        env_path = tmp_path / ".env"
        env_path.write_text("MEMOLOUPE_ASR__APIKEY=sk-from-env\n", encoding="utf-8")
        loaded = load_env_file(env_path)
        config = load_config(env={**loaded, "MEMOLOUPE_ASR__BASEURL": "http://x"})
        assert config["asr"]["apiKey"] == "sk-from-env"


class TestAsrLocalConfig:
    def test_asr_local_config_defaults_present(self) -> None:
        asr = DEFAULT_CONFIG["asr"]
        assert asr["language"] is None
        assert asr["localAsrVersion"] == "asr-local.v1"
        assert asr["vad"]["speechThreshold"] == 0.4
        assert asr["vad"]["modelDir"] is None
        assert asr["whisper"]["model"] == "mlx-community/whisper-large-v3-turbo"
        assert asr["whisper"]["wordTimestamps"] is True
        assert asr["mergeGapMs"] == 300
        assert asr["windowSec"] == 30
        assert asr["windowPadMs"] == 200

    def test_asr_local_env_override_nested(self) -> None:
        config = load_config(
            env={
                "MEMOLOUPE_ASR__PROVIDER": "local-fireredvad-mlx",
                "MEMOLOUPE_ASR__VAD__SPEECHTHRESHOLD": "0.5",
                "MEMOLOUPE_ASR__WHISPER__MODEL": "mlx-community/whisper-tiny",
                "MEMOLOUPE_ASR__MERGEGAPMS": "500",
            }
        )
        assert config["asr"]["provider"] == "local-fireredvad-mlx"
        assert config["asr"]["vad"]["speechThreshold"] == 0.5
        assert config["asr"]["whisper"]["model"] == "mlx-community/whisper-tiny"
        assert config["asr"]["mergeGapMs"] == 500

    def test_asr_fingerprint_changes_with_local_config(self) -> None:
        base = load_config(env={})
        changed = load_config(
            env={"MEMOLOUPE_ASR__WHISPER__MODEL": "mlx-community/whisper-tiny"}
        )
        assert config_fingerprint(base, ["asr"]) != config_fingerprint(
            changed, ["asr"]
        )
