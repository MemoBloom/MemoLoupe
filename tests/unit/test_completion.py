"""analysis.completion 单元测试：completion 规则评估与文档确认（docs/04 §2/§9）。

覆盖：requiredFields / requireVerifiedStates / allowUnknown /
requireValidEvidenceRefs 各规则的命中与未满足定位；confirm_document 的
前置校验（completion、跨文件校验、HTML 校验）、outdated 拒绝与
confirmedAt/confirmedBy 原子写入。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from memoloupe.analysis.completion import (
    COMPLETION_VERSION,
    CompletionReport,
    confirm_document,
    evaluate_completion,
)
from memoloupe.analysis.observations import (
    Confidence,
    Observation,
    Source,
    model_value_observation,
    unknown_observation,
    unmapped_observation,
)
from memoloupe.core.atomic_io import read_json, write_json_atomic
from memoloupe.render.corrections import append_changes, load_corrections
from memoloupe.render.shot_html import render_shot_html

FIXTURE_FULL = Path(__file__).parent.parent / "fixtures" / "output_full"
RULES_PATH = Path(__file__).parent.parent.parent / "rules" / "completion.json"
REF = "raw/unified-media.json#batches[0].response.shots[0].visual.framing"

RULES = json.loads(RULES_PATH.read_text(encoding="utf-8"))


def _value_obs(field: str, shot_id: str, value: object = "全景") -> Observation:
    return model_value_observation(field, shot_id, value, evidence_refs=(REF,))


def _satisfied_observations() -> list[Observation]:
    """两个镜头、requiredFields 全部为 value 的观察集。"""
    obs: list[Observation] = []
    for sid in ("SH0001", "SH0002"):
        for field in ("visual.contentSummary", "visual.framing", "audio.speech"):
            obs.append(_value_obs(field, sid))
    return obs


def _copy_fixture(tmp_path: Path) -> Path:
    work = tmp_path / "out"
    shutil.copytree(FIXTURE_FULL, work)
    return work


class TestEvaluateCompletion:
    def test_version_constant(self):
        assert COMPLETION_VERSION == "completion.v1"

    def test_all_required_fields_value_satisfied(self):
        report = evaluate_completion("shotAnalysis", _satisfied_observations(), RULES)
        assert report.satisfied is True
        assert report.unmet == ()
        assert report.document_type == "shotAnalysis"

    def test_missing_required_field_unmet_with_location(self):
        obs = [o for o in _satisfied_observations() if o.field != "visual.framing"]
        report = evaluate_completion("shotAnalysis", obs, RULES)
        assert report.satisfied is False
        assert any("visual.framing" in u for u in report.unmet)
        assert any("SH0001" in u for u in report.unmet)

    def test_unverified_require_verified_state_unmet(self):
        obs = _satisfied_observations()
        obs.append(
            unmapped_observation(
                "visual.dominantColor", "SH0001", "说不上来的颜色", evidence_refs=(REF,)
            )
        )
        report = evaluate_completion("shotAnalysis", obs, RULES)
        assert report.satisfied is False
        assert any("visual.dominantColor" in u and "SH0001" in u for u in report.unmet)

    def test_verified_require_verified_state_ok(self):
        obs = _satisfied_observations()
        mapped = unmapped_observation(
            "visual.dominantColor", "SH0001", "说不上来的颜色", evidence_refs=(REF,)
        )
        obs.append(
            Observation(
                field=mapped.field,
                shot_id=mapped.shot_id,
                value=mapped.value,
                state=mapped.state,
                confidence=Confidence.UNKNOWN,
                evidence_refs=mapped.evidence_refs,
                source=Source.HUMAN,
                verified=True,
                original_value=mapped.original_value,
            )
        )
        report = evaluate_completion("shotAnalysis", obs, RULES)
        assert report.satisfied is True

    def test_unknown_allowed_when_allow_unknown_true(self):
        obs = _satisfied_observations()
        obs.append(unknown_observation("visual.imageTexture", "SH0001"))
        report = evaluate_completion("shotAnalysis", obs, RULES)
        assert report.satisfied is True

    def test_unknown_unmet_when_allow_unknown_false(self):
        rules = json.loads(json.dumps(RULES))
        rules["documents"]["shotAnalysis"]["allowUnknown"] = False
        obs = _satisfied_observations()
        obs[0] = unknown_observation("visual.contentSummary", "SH0001")
        report = evaluate_completion("shotAnalysis", obs, rules)
        assert report.satisfied is False
        assert any("visual.contentSummary" in u and "SH0001" in u for u in report.unmet)

    def test_unknown_required_field_allowed_when_allow_unknown_true(self):
        obs = _satisfied_observations()
        obs[0] = unknown_observation("visual.contentSummary", "SH0001")
        report = evaluate_completion("shotAnalysis", obs, RULES)
        assert report.satisfied is True

    def test_invalid_evidence_ref_unmet(self):
        obs = _satisfied_observations()
        obs.append(
            model_value_observation(
                "visual.contentSummary", "SH0001", "重复行", evidence_refs=("/abs/path#x",)
            )
        )
        report = evaluate_completion("shotAnalysis", obs, RULES)
        assert report.satisfied is False
        assert any("SH0001" in u and "visual.contentSummary" in u for u in report.unmet)

    def test_unknown_state_exempt_from_evidence_ref_check(self):
        obs = _satisfied_observations()
        obs.append(unknown_observation("visual.imageTexture", "SH0001"))
        report = evaluate_completion("shotAnalysis", obs, RULES)
        assert report.satisfied is True

    def test_document_without_rules_satisfied(self):
        report = evaluate_completion("storyAnalysis", [], RULES)
        assert report.satisfied is True
        assert report.unmet == ()

    def test_report_is_frozen_dataclass(self):
        report = evaluate_completion("shotAnalysis", _satisfied_observations(), RULES)
        assert isinstance(report, CompletionReport)
        assert isinstance(report.unmet, tuple)


class TestConfirmDocument:
    def test_confirm_happy_path_writes_confirmed_fields(self, tmp_path):
        work = _copy_fixture(tmp_path)
        # 人工核实三个镜头的 absent-claimed（components.nonTextOverlayEvents），
        # 使 completion 满足后再确认。
        changes = [
            {
                "entityID": sid,
                "field": "components.nonTextOverlayEvents",
                "oldValue": None,
                "newValue": None,
                "state": "absent-claimed",
                "verified": True,
            }
            for sid in ("SH0001", "SH0002", "SH0003")
        ]
        append_changes(work, "shotAnalysis", "a1b2c3d4e5f6", changes)
        render_shot_html(work)
        ok, reasons = confirm_document(work, "shotAnalysis", actor="human")
        assert ok, reasons
        assert reasons == []
        corrections = load_corrections(work, "shotAnalysis")
        assert corrections.confirmed_at is not None
        assert corrections.confirmed_by == "human"
        # 确认写入保留历史 changes。
        assert len(corrections.changes) == 3
        # 写入的文件仍通过 schema 校验（load 内部校验）。
        on_disk = read_json(work / "corrections" / "shotAnalysis.json")
        assert on_disk["documentType"] == "shotAnalysis"

    def test_confirm_rejects_when_completion_unmet(self, tmp_path):
        work = _copy_fixture(tmp_path)
        # 把一个 requiredField 改成词表外值 → unmapped，completion 不满足。
        unified_path = work / "raw" / "unified-media.json"
        unified = read_json(unified_path)
        unified["batches"][0]["response"]["shots"][0]["visual"]["framing"] = "奇异角度!!"
        write_json_atomic(unified_path, unified)
        render_shot_html(work)
        ok, reasons = confirm_document(work, "shotAnalysis")
        assert ok is False
        assert reasons
        assert not (work / "corrections" / "shotAnalysis.json").exists()

    def test_confirm_rejects_when_html_missing(self, tmp_path):
        work = _copy_fixture(tmp_path)
        ok, reasons = confirm_document(work, "shotAnalysis")
        assert ok is False
        assert any("shot-analysis.html" in r for r in reasons)
        assert not (work / "corrections").exists()

    def test_confirm_rejects_outdated(self, tmp_path):
        work = _copy_fixture(tmp_path)
        render_shot_html(work)
        append_changes(work, "shotAnalysis", "old-revision", [
            {
                "entityID": "SH0001",
                "field": "visual.framing",
                "oldValue": "全景",
                "newValue": "中景",
                "state": "value",
                "verified": True,
            }
        ])
        ok, reasons = confirm_document(work, "shotAnalysis")
        assert ok is False
        assert any("outdated" in r for r in reasons)
        corrections = load_corrections(work, "shotAnalysis")
        assert corrections.confirmed_at is None
