"""``memoloupe profile`` CLI 集成测试（roadmap 04-03）。

覆盖：输入门禁、无模型/Mock 蒸馏两条链路、checkpoint 复用、
validate 对 style-profile.json 的闭环。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from memoloupe.cli.main import (
    EXIT_INPUT,
    EXIT_OK,
    EXIT_STAGE_FAILED,
    EXIT_USAGE,
    EXIT_VALIDATION_FAILED,
    main,
)
from memoloupe.services.base import PermanentServiceError
from memoloupe.services.mock import MockTextModelService

FIXTURE_FULL = Path(__file__).parent.parent / "fixtures" / "output_full"


class TestProfileCLI:
    def test_profile_without_model_produces_aggregate(self, tmp_path):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        assert main(["profile", "--output-dir", str(work)]) == EXIT_OK
        profile = json.loads((work / "style-profile.json").read_text(encoding="utf-8"))
        assert profile["distillStatus"] == "skipped"
        assert profile["schemaVersion"] == 2
        assert [s["slotId"] for s in profile["structure"]["slots"]] == ["S001", "S002"]

    def test_profile_with_mock_distill(self, tmp_path):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        assert main(["profile", "--output-dir", str(work), "--mock-text-model"]) == EXIT_OK
        profile = json.loads((work / "style-profile.json").read_text(encoding="utf-8"))
        assert profile["distillStatus"] == "complete"
        slot = profile["structure"]["slots"][0]
        assert slot["L1"]["functionalTitle"] is not None
        assert slot["L1"]["narrativeFunction"] is not None
        assert slot["L2"]["referenceContent"] != ""
        assert (work / "checkpoints" / "style-profile-distill.json").is_file()

    def test_skip_distill_produces_aggregate(self, tmp_path):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        assert main(["profile", "--output-dir", str(work), "--skip-distill"]) == EXIT_OK
        profile = json.loads((work / "style-profile.json").read_text(encoding="utf-8"))
        assert profile["distillStatus"] == "skipped"

    def test_skip_distill_conflicts_with_mock(self, tmp_path):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        code = main([
            "profile", "--output-dir", str(work),
            "--skip-distill", "--mock-text-model",
        ])
        assert code == EXIT_USAGE

    def test_strict_returns_failed_on_text_model_failure(self, tmp_path, monkeypatch):
        import memoloupe.cli.profile_build as profile_cli

        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        service = MockTextModelService({0: PermanentServiceError("HTTP 401")})
        monkeypatch.setattr(
            profile_cli,
            "build_text_model_service",
            lambda config: (service, None),
        )
        code = main([
            "profile", "--output-dir", str(work),
            "--strict", "--force", "profile_distill",
        ])
        assert code == EXIT_STAGE_FAILED

    def test_rerun_reuses_checkpoint(self, tmp_path):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        assert main(["profile", "--output-dir", str(work), "--mock-text-model"]) == EXIT_OK
        first = (work / "style-profile.json").read_text(encoding="utf-8")
        assert main(["profile", "--output-dir", str(work), "--mock-text-model"]) == EXIT_OK
        second = (work / "style-profile.json").read_text(encoding="utf-8")
        assert first == second

    def test_missing_story_blocks_is_input_error(self, tmp_path, capsys):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        (work / "raw" / "story-blocks.json").unlink()
        code = main(["profile", "--output-dir", str(work)])
        assert code == EXIT_INPUT
        assert "输入不可用" in capsys.readouterr().err

    def test_validate_strict_passes_after_profile(self, tmp_path):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        assert main(["profile", "--output-dir", str(work), "--mock-text-model"]) == EXIT_OK
        assert main(["validate", str(work), "--strict"]) == EXIT_OK

    def test_broken_profile_fails_validate(self, tmp_path, capsys):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        assert main(["profile", "--output-dir", str(work)]) == EXIT_OK
        path = work / "style-profile.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["structure"]["slots"][0]["L1"]["durationShare"] = 0.9
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        code = main(["validate", str(work), "--strict"])
        assert code == EXIT_VALIDATION_FAILED
        assert "style-profile" in capsys.readouterr().out

    def test_validate_rejects_broken_hook_references(self, tmp_path, capsys):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        assert main(["profile", "--output-dir", str(work), "--mock-text-model"]) == EXIT_OK
        path = work / "style-profile.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["structure"]["hook"] = {
            "L1": {"atSeconds": 0.0, "slotId": "S001", "blockId": "B0002"},
            "L2": {"form": "错误引用", "referenceContent": "block 与 slot 不一致。"},
            "L3": {"shotIds": ["SH9999"]},
        }
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        code = main(["validate", str(work), "--strict"])
        assert code == EXIT_VALIDATION_FAILED
        out = capsys.readouterr().out
        assert "style-profile" in out
        assert "hook" in out
