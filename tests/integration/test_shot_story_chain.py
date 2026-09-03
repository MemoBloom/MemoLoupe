"""shot+story 合并流程（D-056）：shot 成功后默认链式执行 story。

全部拦截 pipeline/story 入口，只验证 CLI 编排与参数透传，不跑真实分析。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import memoloupe.cli.shot_analysis as shot_cli
from memoloupe.analysis.shot_pipeline import PipelineReport
from memoloupe.cli.main import EXIT_OK, EXIT_STAGE_FAILED


@pytest.fixture
def chained(monkeypatch, tmp_path):
    """拦截 ShotAnalysisPipeline.run 与链式 run_story_analysis，捕获调用参数。"""
    captured: dict = {}

    def fake_run(self, request):
        captured["shot_request"] = request
        status = captured.get("shot_status", "complete")
        return PipelineReport(
            phase="shot",
            status=status,
            steps=[],
            warnings=[],
            artifacts=[],
            elapsed_ms=0,
        )

    def fake_story(argv):
        captured["story_argv"] = list(argv)
        return captured.get("story_code", EXIT_OK)

    monkeypatch.setattr(shot_cli.ShotAnalysisPipeline, "run", fake_run)
    monkeypatch.setattr(shot_cli, "run_story_analysis", fake_story)
    monkeypatch.setattr(shot_cli, "_tool_available", lambda binary: True)
    source = tmp_path / "video.mp4"
    source.write_bytes(b"fake")
    return captured, source


def _run(source: Path, tmp_path: Path, *extra: str) -> int:
    return shot_cli.run_shot_analysis(
        [str(source), "--output-dir", str(tmp_path / "out"), *extra]
    )


class TestShotStoryChain:
    def test_default_chains_story_with_allow_draft(self, chained, tmp_path):
        captured, source = chained
        code = _run(source, tmp_path)
        assert code == EXIT_OK
        argv = captured["story_argv"]
        assert "--allow-draft" in argv
        assert "--gap-ms" in argv and argv[argv.index("--gap-ms") + 1] == "2000"
        assert "--output-dir" in argv

    def test_skip_story_opts_out(self, chained, tmp_path):
        captured, source = chained
        code = _run(source, tmp_path, "--skip-story")
        assert code == EXIT_OK
        assert "story_argv" not in captured

    def test_story_failure_code_propagates(self, chained, tmp_path):
        captured, source = chained
        captured["story_code"] = EXIT_STAGE_FAILED
        assert _run(source, tmp_path) == EXIT_STAGE_FAILED

    def test_shot_failure_skips_story(self, chained, tmp_path):
        captured, source = chained
        captured["shot_status"] = "failed"
        assert _run(source, tmp_path) == EXIT_STAGE_FAILED
        assert "story_argv" not in captured

    def test_mock_services_maps_to_mock_text_model(self, chained, tmp_path):
        captured, source = chained
        assert _run(source, tmp_path, "--mock-services") == EXIT_OK
        assert "--mock-text-model" in captured["story_argv"]

    def test_dry_run_maps_to_scaffold_only(self, chained, tmp_path):
        captured, source = chained
        assert _run(source, tmp_path, "--dry-run") == EXIT_OK
        assert "--scaffold-only" in captured["story_argv"]

    def test_gap_ms_and_strict_forwarded(self, chained, tmp_path):
        captured, source = chained
        assert _run(source, tmp_path, "--gap-ms", "2000", "--strict") == EXIT_OK
        argv = captured["story_argv"]
        assert argv[argv.index("--gap-ms") + 1] == "2000"
        assert "--strict" in argv

    def test_render_only_does_not_chain(self, chained, tmp_path, monkeypatch):
        captured, source = chained
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        monkeypatch.setattr(shot_cli, "render_shot_html", lambda out: out)
        code = shot_cli.run_shot_analysis(
            [str(source), "--output-dir", str(out_dir), "--render-only"]
        )
        assert code == EXIT_OK
        assert "story_argv" not in captured
