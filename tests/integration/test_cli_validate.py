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
        # story 已实现：缺 --output-dir 是参数错误（退出码 2）。
        with pytest.raises(SystemExit) as exc_info:
            main(["story"])
        assert exc_info.value.code == 2

    def test_story_command_missing_dir_is_input_error(self, tmp_path, capsys):
        assert main(["story", "--output-dir", str(tmp_path / "nonexistent")]) == EXIT_INPUT
        assert "不存在" in capsys.readouterr().err

    def test_profile_command_missing_dir_is_input_error(self, tmp_path, capsys):
        assert main(["profile", "--output-dir", str(tmp_path / "nonexistent")]) == EXIT_INPUT
        assert "不存在" in capsys.readouterr().err

    def test_profile_command_missing_inputs_is_input_error(self, tmp_path, capsys):
        work = tmp_path / "out"
        work.mkdir()
        assert main(["profile", "--output-dir", str(work)]) == EXIT_INPUT
        assert "输入不可用" in capsys.readouterr().err
