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
        assert DEFAULT_CONFIG["story"]["gapMs"] == 1200
        assert DEFAULT_CONFIG["unifiedModel"]["batchSize"] == 4
        assert DEFAULT_CONFIG["unifiedModel"]["concurrency"] == 10
        assert DEFAULT_CONFIG["unifiedModel"]["videoFPS"] == 10.0
        assert DEFAULT_CONFIG["unifiedModel"]["mediaResolution"] == "default"
        assert DEFAULT_CONFIG["unifiedModel"]["maxRetries"] == 3
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
        }
        config = load_config(env=env)
        assert config["quality"]["blurFlagThreshold"] == 13.5
        assert config["music"]["enabled"] is False

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
