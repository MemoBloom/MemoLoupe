"""CLI validate 集成测试：对真实 fixture output-dir 跑主命令。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from memoloupe.cli.main import (
    EXIT_INPUT,
    EXIT_OK,
    EXIT_STAGE_FAILED,
    EXIT_VALIDATION_FAILED,
    main,
)

FIXTURE_FULL = Path(__file__).parent.parent / "fixtures" / "output_full"
FIXTURE_MINIMAL = Path(__file__).parent.parent / "fixtures" / "minimal"


class TestValidateCommand:
    def test_full_fixture_passes(self, capsys):
        assert main(["validate", str(FIXTURE_FULL)]) == EXIT_OK
        out = capsys.readouterr().out
        assert "0 个错误" in out

    def test_full_fixture_passes_strict(self):
        assert main(["validate", str(FIXTURE_FULL), "--strict"]) == EXIT_OK

    def test_minimal_fixture_passes(self):
        assert main(["validate", str(FIXTURE_MINIMAL)]) == EXIT_OK

    def test_json_report_is_machine_readable(self, capsys):
        assert main(["validate", str(FIXTURE_FULL), "--json-report"]) == EXIT_OK
        report = json.loads(capsys.readouterr().out)
        assert report["errorCount"] == 0
        assert isinstance(report["issues"], list)

    def test_missing_dir_is_input_error(self, tmp_path, capsys):
        code = main(["validate", str(tmp_path / "nonexistent")])
        assert code == EXIT_INPUT
        assert "不存在" in capsys.readouterr().err

    def test_broken_artifact_fails_validation(self, tmp_path, capsys):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        # 破坏跨文件一致性：把 audio-cuts 的一个 shotID 改成未知镜头
        audio_cuts = work / "raw" / "audio-cuts.json"
        data = json.loads(audio_cuts.read_text(encoding="utf-8"))
        data["shots"][0]["shotID"] = "SH9999"
        audio_cuts.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        code = main(["validate", str(work)])
        assert code == EXIT_VALIDATION_FAILED
        assert "SH9999" in capsys.readouterr().out


class TestUnimplementedCommands:
    def test_shot_command_missing_input_is_input_error(self, capsys):
        # shot 已实现：不存在的输入返回输入错误（退出码 3）
        assert main(["shot", "input.mp4", "--output-dir", "out"]) == EXIT_INPUT
        assert "不存在" in capsys.readouterr().err

    def test_story_command_missing_output_dir_is_usage_error(self, capsys):
        # shot --story-only（承接原 story）：缺 --output-dir 是参数错误（退出码 2）。
        with pytest.raises(SystemExit) as exc_info:
            main(["shot", "--story-only"])
        assert exc_info.value.code == 2

    def test_story_command_missing_dir_is_input_error(self, tmp_path, capsys):
        assert main(["shot", "--story-only", "--output-dir", str(tmp_path / "nonexistent")]) == EXIT_INPUT
        assert "不存在" in capsys.readouterr().err

    def test_profile_command_missing_dir_is_input_error(self, tmp_path, capsys):
        assert main(["profile", "--output-dir", str(tmp_path / "nonexistent")]) == EXIT_INPUT
        assert "不存在" in capsys.readouterr().err

    def test_profile_command_missing_inputs_is_input_error(self, tmp_path, capsys):
        work = tmp_path / "out"
        work.mkdir()
        assert main(["profile", "--output-dir", str(work)]) == EXIT_INPUT
        assert "输入不可用" in capsys.readouterr().err


class TestEnvFileAndConfigCommand:
    """05-05：--env-file 加载与 memoloupe config --print。"""

    def test_config_print_redacts_secrets(self, capsys, monkeypatch):
        monkeypatch.setenv("MEMOLOUPE_ASR__APIKEY", "sk-print-secret")
        assert main(["config"]) == EXIT_OK
        captured = capsys.readouterr()
        report = json.loads(captured.out)
        assert report["asr"]["apiKey"] == "***"
        assert "sk-print-secret" not in captured.err + json.dumps(report)

    def test_config_print_accepts_local_asr_without_remote_credentials(
        self, capsys, monkeypatch
    ):
        monkeypatch.setenv(
            "MEMOLOUPE_ASR__PROVIDER", "local-fireredvad-mlx"
        )
        monkeypatch.setenv("MEMOLOUPE_ASR__ENABLED", "true")
        monkeypatch.setenv(
            "MEMOLOUPE_ASR__WHISPER__MODEL",
            "mlx-community/whisper-large-v3-turbo",
        )
        for key in (
            "MEMOLOUPE_ASR__BASEURL",
            "MEMOLOUPE_ASR__APIKEY",
            "MEMOLOUPE_ASR__MODEL",
        ):
            monkeypatch.delenv(key, raising=False)

        assert main(["config"]) == EXIT_OK

        captured = capsys.readouterr()
        assert "ASR" not in captured.err

    def test_config_print_reports_local_asr_without_whisper_model(
        self, capsys, monkeypatch
    ):
        monkeypatch.setenv(
            "MEMOLOUPE_ASR__PROVIDER", "local-fireredvad-mlx"
        )
        monkeypatch.setenv("MEMOLOUPE_ASR__ENABLED", "true")
        monkeypatch.setenv("MEMOLOUPE_ASR__WHISPER__MODEL", "")

        assert main(["config"]) == EXIT_OK

        captured = capsys.readouterr()
        assert "未配置的真实服务：" in captured.err
        assert "ASR" in captured.err

    def test_env_file_feeds_story_gate(self, tmp_path, capsys):
        # story 输入门禁不读 API key；用 config 子命令验证 --env-file 生效。
        env_file = tmp_path / ".env"
        env_file.write_text(
            "MEMOLOUPE_ASR__APIKEY=sk-from-env-file\n", encoding="utf-8"
        )
        assert main(["--env-file", str(env_file), "config"]) == EXIT_OK
        report = json.loads(capsys.readouterr().out)
        assert report["asr"]["apiKey"] == "***"

    def test_env_file_does_not_override_process_env(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("MEMOLOUPE_ASR__APIKEY", "sk-process-env")
        env_file = tmp_path / ".env"
        env_file.write_text(
            "MEMOLOUPE_ASR__APIKEY=sk-file-env\n", encoding="utf-8"
        )
        assert main(["--env-file", str(env_file), "config"]) == EXIT_OK
        report = json.loads(capsys.readouterr().out)
        # redacted_snapshot 只输出 ***，无法直接区分来源；改为验证
        # load_env_file 不覆盖（单元测试已覆盖），此处只验证命令可用。
        assert report["asr"]["apiKey"] == "***"
