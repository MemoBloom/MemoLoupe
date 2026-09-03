"""``memoloupe story`` CLI 集成测试（roadmap 03-04）。

覆盖：默认门禁（shot analysis 可用性）、--allow-draft 降级、mock 文本模型
纵向链路、story HTML 渲染、validate 对 story JSON + story HTML 的闭环。
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
FIXTURE_MINIMAL = Path(__file__).parent.parent / "fixtures" / "minimal"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestStoryCLI:
    def test_story_on_full_fixture_renders_html(self, tmp_path):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        code = main(["story", "--output-dir", str(work), "--allow-draft"])
        assert code == EXIT_OK
        assert (work / "raw" / "story-blocks.json").is_file()
        assert (work / "story-analysis.html").is_file()
        html = (work / "story-analysis.html").read_text(encoding="utf-8")
        assert 'data-document-type="storyAnalysis"' in html

    def test_story_default_rejects_unconfirmed_shot_analysis(self, tmp_path, capsys):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        code = main(["story", "--output-dir", str(work)])
        assert code == EXIT_INPUT
        err = capsys.readouterr().err
        assert "尚未 confirmed" in err
        assert "--allow-draft" in err

    def test_story_default_accepts_confirmed_shot_analysis(self, tmp_path):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        revision = _read_json(work / "raw" / "media.json")["source"]["revisionID"]
        corr_dir = work / "corrections"
        corr_dir.mkdir()
        (corr_dir / "shotAnalysis.json").write_text(
            json.dumps(
                {
                    "correctionVersion": 1,
                    "documentType": "shotAnalysis",
                    "sourceRevisionID": revision,
                    "changes": [],
                    "confirmedAt": "2026-08-25T00:00:00Z",
                    "confirmedBy": "tester",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        assert main(["story", "--output-dir", str(work), "--mock-text-model"]) == EXIT_OK

    def test_story_with_mock_text_model_fills_slots(self, tmp_path):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        code = main([
            "story", "--output-dir", str(work), "--mock-text-model", "--allow-draft",
        ])
        assert code == EXIT_OK
        blocks = _read_json(work / "raw" / "story-blocks.json")
        assert blocks["status"] == "complete"
        assert blocks["slots"], "mock 填充应产出 slot"
        assert (work / "checkpoints" / "story-blocks-model.json").is_file()

    def test_scaffold_only_skips_text_model(self, tmp_path):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        code = main([
            "story", "--output-dir", str(work), "--allow-draft",
            "--scaffold-only", "--force", "scaffold_story_blocks",
        ])
        assert code == EXIT_OK
        blocks = _read_json(work / "raw" / "story-blocks.json")
        assert blocks["status"] == "scaffold"
        assert blocks["slots"] == []

    def test_scaffold_only_conflicts_with_mock(self, tmp_path):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        code = main([
            "story", "--output-dir", str(work), "--allow-draft",
            "--scaffold-only", "--mock-text-model",
        ])
        assert code == EXIT_USAGE

    def test_strict_returns_failed_on_text_model_failure(self, tmp_path, monkeypatch):
        import memoloupe.cli.story_analysis as story_cli

        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        service = MockTextModelService({0: PermanentServiceError("HTTP 401")})
        monkeypatch.setattr(
            story_cli,
            "build_text_model_service",
            lambda config: (service, None),
        )
        code = main([
            "story", "--output-dir", str(work), "--allow-draft",
            "--strict", "--force", "story_model_fill",
        ])
        assert code == EXIT_STAGE_FAILED

    def test_story_rerun_reuses_checkpoint(self, tmp_path):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        assert main([
            "story", "--output-dir", str(work), "--mock-text-model", "--allow-draft",
        ]) == EXIT_OK
        first = _read_json(work / "raw" / "story-blocks.json")
        assert main([
            "story", "--output-dir", str(work), "--mock-text-model", "--allow-draft",
        ]) == EXIT_OK
        second = _read_json(work / "raw" / "story-blocks.json")
        assert second["generatedAt"] == first["generatedAt"]

    def test_story_rerenders_merged_workbench(self, tmp_path):
        """story 完成后重渲 shot 工作台：故事轨道进入 shot-analysis.html（D-051）。"""
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        assert main([
            "story", "--output-dir", str(work), "--mock-text-model", "--allow-draft",
        ]) == EXIT_OK
        html = (work / "shot-analysis.html").read_text(encoding="utf-8")
        assert 'id="story-timeline-band"' in html

    def test_missing_shot_analysis_is_input_error(self, tmp_path, capsys):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        (work / "raw" / "shots.json").unlink()
        code = main(["story", "--output-dir", str(work)])
        assert code == EXIT_INPUT
        assert "shot analysis 不可用" in capsys.readouterr().err

    def test_allow_draft_skips_gate_but_pipeline_fails_explicitly(self, tmp_path):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        (work / "raw" / "shots.json").unlink()
        code = main(["story", "--output-dir", str(work), "--allow-draft"])
        assert code == EXIT_STAGE_FAILED

    def test_validate_checks_story_html(self, tmp_path):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        assert main(["story", "--output-dir", str(work), "--allow-draft"]) == EXIT_OK
        html_path = work / "story-analysis.html"
        text = html_path.read_text(encoding="utf-8")
        # 破坏一个五态单元格：去掉 data-value-state。
        assert "data-value-state=" in text
        html_path.write_text(
            text.replace("data-value-state=", "data-value-status=", 1), encoding="utf-8"
        )
        code = main(["validate", str(work)])
        assert code == EXIT_VALIDATION_FAILED

    def test_validate_strict_passes_full_story_chain(self, tmp_path):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        assert main([
            "story", "--output-dir", str(work), "--mock-text-model", "--allow-draft",
        ]) == EXIT_OK
        assert not (work / "style-profile.json").exists()
        assert list((work / "checkpoints" / "outdated").glob("style-profile.*.json"))
        assert main(["validate", str(work), "--strict"]) == EXIT_OK

    def test_json_report_includes_story_html(self, tmp_path, capsys):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        code = main(["story", "--output-dir", str(work), "--json-report", "--allow-draft"])
        assert code == EXIT_OK
        report = json.loads(capsys.readouterr().out)
        assert report["phase"] == "story"
        assert report["storyHtml"] == "story-analysis.html"
