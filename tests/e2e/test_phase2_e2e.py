"""Phase 2 端到端测试（roadmap 03-04 Phase 03 E2E）。

纵向链路：Phase 1 fixture/output → story scaffold → Mock 文本模型填充 →
raw/story-blocks.json → story-analysis.html → memoloupe validate --strict。

场景：
1. 完整 Mock 样例：output_full fixture → scaffold + mock fill（complete）→
   story HTML → strict 校验 0 error；
2. 最小样例：minimal fixture → scaffold（无模型）→ story HTML → strict 校验 0 error；
3. 模型失败降级：mock 抛永久错误 → 保留 scaffold → story HTML 仍渲染 →
   strict 校验 0 error；
4. 同配置重跑复用 checkpoint，不重发模型请求。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from memoloupe.analysis.story_pipeline import (
    StoryAnalysisPipeline,
    StoryAnalysisRequest,
)
from memoloupe.cli.main import EXIT_OK, main
from memoloupe.render.story_html import render_story_html
from memoloupe.services.base import PermanentServiceError
from memoloupe.services.mock import MockTextModelService
from memoloupe.validate.cross_artifact import validate_output_dir
from memoloupe.validate.html_contract import validate_html

FIXTURE_FULL = Path(__file__).resolve().parent.parent / "fixtures" / "output_full"
FIXTURE_MINIMAL = Path(__file__).resolve().parent.parent / "fixtures" / "minimal"


def _strict_errors(out_dir: Path) -> list:
    issues = list(validate_output_dir(out_dir, strict=True))
    story_html = out_dir / "story-analysis.html"
    if story_html.is_file():
        issues.extend(validate_html(story_html, root=out_dir, strict=True))
    return [i for i in issues if i.severity == "error"]


def _full_fixture_copy(tmp_path: Path) -> Path:
    work = tmp_path / "out"
    shutil.copytree(FIXTURE_FULL, work)
    return work


def _prompt_block_ids(prompt: str) -> list[str]:
    return re.findall(r"^##\s+(B\d{4})\b", prompt, flags=re.MULTILINE)


def _mock_fill_service() -> MockTextModelService:
    """callable mock：按 prompt 中的块 ID 回填合法叙事字段 + 一个 slot。"""

    def respond(request):
        block_ids = _prompt_block_ids(request.prompt)
        assert block_ids, "prompt 应包含确定性块"
        return json.dumps(
            {
                "blocks": [
                    {
                        "storyBlockID": bid,
                        "blockTitle": "演示块",
                        "divisionAxis": "行动/任务",
                        "divisionRationale": "同一行动段落。",
                        "primaryRole": "development",
                        "coreContent": "演示用核心内容。",
                        "informationRole": "推进新信息",
                        "narrativeDensity": "中",
                        "audienceReaction": "获得信息/学到东西",
                        "visualIndependence": "静音也能看懂",
                        "blockRelation": "",
                        "relationReason": "",
                    }
                    for bid in block_ids
                ],
                "slots": [
                    {
                        "slotID": "S001",
                        "slotType": "行动展开",
                        "slotTitle": "演示槽",
                        "blockIDs": block_ids,
                        "slotRationale": "全部块聚合为一个演示 slot。",
                    }
                ],
            },
            ensure_ascii=False,
        )

    return MockTextModelService(respond)


class TestPhase2FullMockChain:
    """场景 1：完整 Mock 样例纵向链路。"""

    def test_full_chain_passes_strict(self, tmp_path):
        work = _full_fixture_copy(tmp_path)
        report = StoryAnalysisPipeline().run(
            StoryAnalysisRequest(output_dir=work, text_service=_mock_fill_service())
        )
        assert report.status == "complete"

        story = json.loads(
            (work / "raw" / "story-blocks.json").read_text(encoding="utf-8")
        )
        assert story["status"] == "complete"
        assert story["boundarySource"] == "asr-gap"
        assert story["slots"], "mock 填充必须产出 slot"
        # fixture ASR 两段间隔 1040ms < gapMs=1200 → 确定性聚块只有 1 块。
        assert [b["storyBlockID"] for b in story["blocks"]] == ["B0001"]
        assert story["blocks"][0]["shotIDs"] == ["SH0001", "SH0002", "SH0003"]

        render_story_html(work)
        html = (work / "story-analysis.html").read_text(encoding="utf-8")
        assert 'data-document-type="storyAnalysis"' in html
        assert 'class="story-block" data-story-block-id="B0001"' in html
        assert 'data-slot-id="S001"' in html
        assert not (work / "style-profile.json").exists()
        assert list((work / "checkpoints" / "outdated").glob("style-profile.*.json"))
        assert _strict_errors(work) == []


class TestPhase2MinimalScaffold:
    """场景 2：最小样例 scaffold（无模型）也通过严格校验。"""

    def test_minimal_scaffold_passes_strict(self, tmp_path):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_MINIMAL, work)

        report = StoryAnalysisPipeline().run(
            StoryAnalysisRequest(output_dir=work)
        )
        assert report.status == "complete"
        story = json.loads(
            (work / "raw" / "story-blocks.json").read_text(encoding="utf-8")
        )
        assert story["status"] == "scaffold"
        assert [b["storyBlockID"] for b in story["blocks"]] == ["B0001"]
        assert story["slots"] == []

        render_story_html(work)
        html = (work / "story-analysis.html").read_text(encoding="utf-8")
        # scaffold 的叙事字段必须呈 unknown 状态，不得伪装确定结论。
        assert 'data-value-state="unknown"' in html
        assert "待确认" in html
        assert not (work / "style-profile.json").exists()
        assert _strict_errors(work) == []


class TestPhase2ModelDegradation:
    """场景 3：模型永久失败 → 保留 scaffold，HTML 照常渲染。"""

    def test_permanent_failure_keeps_scaffold_and_html(self, tmp_path):
        work = _full_fixture_copy(tmp_path)
        report = StoryAnalysisPipeline().run(
            StoryAnalysisRequest(
                output_dir=work,
                text_service=MockTextModelService({0: PermanentServiceError("HTTP 401")}),
            )
        )
        assert report.status == "partial"
        story = json.loads(
            (work / "raw" / "story-blocks.json").read_text(encoding="utf-8")
        )
        assert story["status"] == "scaffold"
        assert [b["storyBlockID"] for b in story["blocks"]] == ["B0001"]

        render_story_html(work)
        assert _strict_errors(work) == []


class TestPhase2CheckpointReuse:
    """场景 4：同配置重跑复用 checkpoint，不重发模型请求。"""

    def test_rerun_reuses_checkpoint(self, tmp_path):
        work = _full_fixture_copy(tmp_path)
        service = _mock_fill_service()
        request = StoryAnalysisRequest(output_dir=work, text_service=service)
        assert StoryAnalysisPipeline().run(request).status == "complete"
        assert len(service.calls) == 1

        assert StoryAnalysisPipeline().run(request).status == "complete"
        assert len(service.calls) == 1  # checkpoint 命中，不重发请求


class TestPhase2CliChain:
    """CLI 纵向链路：memoloupe story → validate --strict。"""

    def test_cli_story_then_strict_validate(self, tmp_path):
        work = _full_fixture_copy(tmp_path)
        assert main([
            "story", "--output-dir", str(work), "--mock-text-model", "--allow-draft",
        ]) == EXIT_OK
        assert main(["validate", str(work), "--strict"]) == EXIT_OK
