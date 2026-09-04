"""Phase 3 端到端测试（roadmap 04-03 Phase 04 E2E）。

纵向链路：Phase 1/2 fixture/output → 确定性聚合 → Mock 蒸馏 →
根目录 style-profile.json → memoloupe validate --strict。

场景：
1. 无模型链路：确定性聚合（distillStatus=skipped）→ strict 校验 0 error；
2. Mock 蒸馏链路：aggregate → distill complete → strict 校验 0 error；
3. 模型失败降级：保留确定性聚合 → strict 校验 0 error；
4. CLI 三阶段纵向：shot fixture → story → profile → validate --strict；
5. 确定性统计可复算：durationShare/avgShotSeconds 由 raw 重算一致。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from memoloupe.analysis.profile_pipeline import (
    ProfileBuildPipeline,
    ProfileBuildRequest,
)
from memoloupe.cli.main import EXIT_OK, main
from memoloupe.core.atomic_io import read_json
from memoloupe.services.base import PermanentServiceError
from memoloupe.services.mock import MockTextModelService
from memoloupe.validate.cross_artifact import validate_output_dir

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "output_full"


def _strict_errors(out_dir: Path) -> list:
    issues = list(validate_output_dir(out_dir, strict=True))
    return [i for i in issues if i.severity == "error"]


def _fixture_copy(tmp_path: Path) -> Path:
    work = tmp_path / "out"
    shutil.copytree(FIXTURE, work)
    return work


def _raws(work: Path) -> dict:
    names = (
        "media", "shots", "story-blocks", "asr", "audio-cuts",
        "music-flags", "camera-motion", "unified-media",
    )
    raws: dict = {}
    for name in names:
        raws[name] = read_json(work / "raw" / f"{name}.json")
    return raws


def _mock_distill_service() -> MockTextModelService:
    """按 prompt 中出现的 slot 行动态回填合法蒸馏响应。"""

    def respond(request):
        slot_ids = re.findall(r"^- (S\d{3}) ", request.prompt, flags=re.MULTILINE)
        assert slot_ids, "prompt 应包含插槽行"
        return json.dumps(
            {
                "slots": [
                    {
                        "slotId": sid,
                        "L1": {
                            "functionalTitle": f"{sid} 演示功能",
                            "narrativeFunction": "setup" if i == 0 else "progression",
                            "intendedReaction": "获得信息/学到东西",
                        },
                        "L2": {
                            "carriage": "演示承载",
                            "pattern": "演示模式",
                            "referenceContent": f"{sid} 的参考内容摘要。",
                        },
                    }
                    for i, sid in enumerate(slot_ids)
                ],
                "hook": None,
                "payoff": None,
                "structureRequirements": [],
                "adoptionHints": None,
                "discussionItems": [],
            },
            ensure_ascii=False,
        )

    return MockTextModelService(respond)


class TestAggregateChain:
    """场景 1：无模型确定性聚合链路。"""

    def test_aggregate_passes_strict(self, tmp_path):
        work = _fixture_copy(tmp_path)
        report = ProfileBuildPipeline().run(ProfileBuildRequest(output_dir=work))
        assert report.status == "complete"
        profile = json.loads((work / "style-profile.json").read_text(encoding="utf-8"))
        assert profile["distillStatus"] == "skipped"
        assert _strict_errors(work) == []

    def test_deterministic_stats_recomputable(self, tmp_path):
        """场景 5：durationShare/avgShotSeconds 由 raw 重算一致。"""
        work = _fixture_copy(tmp_path)
        ProfileBuildPipeline().run(ProfileBuildRequest(output_dir=work))
        profile = json.loads((work / "style-profile.json").read_text(encoding="utf-8"))
        shots = _raws(work)["shots"]["shots"]
        story = _raws(work)["story-blocks"]
        total = shots[-1]["finalEndMs"] - shots[0]["finalStartMs"]
        block_by_id = {b["storyBlockID"]: b for b in story["blocks"]}
        story_slots = {s["slotID"]: s for s in story["slots"]}
        for slot in profile["structure"]["slots"]:
            blocks = [
                block_by_id[b]
                for b in story_slots[slot["slotId"]]["blockIDs"]
            ]
            span = blocks[-1]["endMs"] - blocks[0]["startMs"]
            assert slot["L1"]["durationShare"] == pytest.approx(span / total, abs=1e-4)
            durations = [
                (s["finalEndMs"] - s["finalStartMs"]) / 1000
                for s in shots if s["shotID"] in slot["L3"]["shotIds"]
            ]
            assert slot["L3"]["avgShotSeconds"] == pytest.approx(
                sum(durations) / len(durations), rel=1e-3
            )


class TestDistillChain:
    """场景 2：Mock 蒸馏链路。"""

    def test_distill_passes_strict(self, tmp_path):
        work = _fixture_copy(tmp_path)
        report = ProfileBuildPipeline().run(
            ProfileBuildRequest(output_dir=work, text_service=_mock_distill_service())
        )
        assert report.status == "complete"
        profile = json.loads((work / "style-profile.json").read_text(encoding="utf-8"))
        assert profile["distillStatus"] == "complete"
        assert profile["structure"]["slots"][0]["L1"]["functionalTitle"]
        # 确定性统计不被模型覆盖。
        assert profile["structure"]["slots"][0]["L3"]["shotCount"] == 2
        assert _strict_errors(work) == []


class TestDistillDegradation:
    """场景 3：模型失败保留确定性聚合。"""

    def test_permanent_failure_keeps_aggregate(self, tmp_path):
        work = _fixture_copy(tmp_path)
        report = ProfileBuildPipeline().run(
            ProfileBuildRequest(
                output_dir=work,
                text_service=MockTextModelService({0: PermanentServiceError("HTTP 401")}),
            )
        )
        assert report.status == "partial"
        profile = json.loads((work / "style-profile.json").read_text(encoding="utf-8"))
        assert profile["distillStatus"] == "skipped"
        assert _strict_errors(work) == []


class TestThreeStageCliChain:
    """场景 4：CLI 三阶段纵向链路。"""

    def test_shot_fixture_to_profile(self, tmp_path):
        work = _fixture_copy(tmp_path)
        # fixture 已是 Phase 1/2 产物；重跑 story 与 profile 验证全链路。
        # fixture 无 confirmed corrections，story 经 shot --story-only --allow-draft。
        assert main(["shot", "--story-only", "--output-dir", str(work), "--mock-text-model", "--allow-draft"]) == EXIT_OK
        assert main(["profile", "--output-dir", str(work), "--mock-text-model"]) == EXIT_OK
        assert main(["validate", str(work), "--strict"]) == EXIT_OK
        profile = json.loads((work / "style-profile.json").read_text(encoding="utf-8"))
        assert profile["distillStatus"] == "complete"
