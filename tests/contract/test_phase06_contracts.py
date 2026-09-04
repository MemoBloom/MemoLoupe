"""契约测试：Phase 06 新增 artifact（review-timeline / shot-relations）。

覆盖：schema 拒绝非法结构；跨文件校验捕获 pair 集合破坏、PTS 逆序、
波形不一致与无证据的 value 指标。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import copy_output_dir, load_fixture, rewrite
from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
from memoloupe.validate.cross_artifact import validate_output_dir

from memoloupe.validate.json_contracts import ValidationIssue


def _errors(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    return [i for i in issues if i.severity == "error"]


def _rt() -> dict:
    return load_fixture("output_full", "raw/review-timeline.json")


def _sr() -> dict:
    return load_fixture("output_full", "raw/shot-relations.json")


class TestReviewTimelineSchema:
    def test_valid_fixture_passes(self) -> None:
        validate_artifact(ArtifactName.REVIEW_TIMELINE, _rt())

    @pytest.mark.parametrize(
        "mutation",
        [
            lambda d: d.update(status="unknown-status"),
            lambda d: d["videoFrames"].update(timingMode="pts-index", ptsMs=None),
            # 注：peaks min>max 无法用 JSON Schema 表达，由跨文件校验器捕尖。
            lambda d: d["waveform"].update(peaks=[[0.2, 1.5]]),
            lambda d: d.update(sourceRevisionID=""),
        ],
    )
    def test_rejects_illegal(self, mutation) -> None:
        doc = _rt()
        mutation(doc)
        with pytest.raises(Exception):
            validate_artifact(ArtifactName.REVIEW_TIMELINE, doc)


class TestShotRelationsSchema:
    def test_valid_fixture_passes(self) -> None:
        validate_artifact(ArtifactName.SHOT_RELATIONS, _sr())

    @pytest.mark.parametrize(
        "mutation",
        [
            lambda d: d["relations"][0].update(pairID="SH0001_SH0002"),
            lambda d: d["relations"][0].update(leftShotID="XX0001"),
            lambda d: d["relations"][0].pop("review"),
            lambda d: d["relations"][0]["review"].pop("reviewReasons"),
            lambda d: d["relations"][0]["semantic"].pop("status"),
            lambda d: d["analysis"].update(pairCount=-1),
        ],
    )
    def test_rejects_illegal(self, mutation) -> None:
        doc = _sr()
        mutation(doc)
        with pytest.raises(Exception):
            validate_artifact(ArtifactName.SHOT_RELATIONS, doc)


class TestCrossArtifactChecks:
    def test_pair_set_mismatch_is_error(self, tmp_path: Path) -> None:
        root = copy_output_dir("output_full", tmp_path)
        doc = load_fixture("output_full", "raw/shot-relations.json")
        doc["relations"][1]["pairID"] = "SH0001--SH0003"
        rewrite(root, "raw/shot-relations.json", doc)
        issues = validate_output_dir(root)
        assert any("严格等于" in i.message for i in _errors(issues))

    def test_boundary_mismatch_is_error(self, tmp_path: Path) -> None:
        root = copy_output_dir("output_full", tmp_path)
        doc = load_fixture("output_full", "raw/shot-relations.json")
        doc["relations"][0]["boundaryMs"] = 9999
        rewrite(root, "raw/shot-relations.json", doc)
        issues = validate_output_dir(root)
        assert any("boundaryMs" in i.json_path for i in _errors(issues))

    def test_value_metric_without_refs_is_error(self, tmp_path: Path) -> None:
        root = copy_output_dir("output_full", tmp_path)
        doc = load_fixture("output_full", "raw/shot-relations.json")
        doc["relations"][0]["metrics"]["lumaDelta"]["evidenceRefs"] = []
        rewrite(root, "raw/shot-relations.json", doc)
        issues = validate_output_dir(root)
        assert any(
            "至少带一个 evidenceRef" in i.message for i in _errors(issues)
        )

    def test_missing_metric_key_is_error(self, tmp_path: Path) -> None:
        root = copy_output_dir("output_full", tmp_path)
        doc = load_fixture("output_full", "raw/shot-relations.json")
        doc["relations"][0]["metrics"].pop("lumaDelta")
        rewrite(root, "raw/shot-relations.json", doc)
        issues = validate_output_dir(root)
        assert any("确定性指标缺失" in i.message for i in _errors(issues))

    def test_pts_regression_is_error(self, tmp_path: Path) -> None:
        root = copy_output_dir("output_full", tmp_path)
        doc = load_fixture("output_full", "raw/review-timeline.json")
        pts = doc["videoFrames"]["ptsMs"]
        doc["videoFrames"]["ptsMs"] = [pts[1], pts[0]] + pts[2:]
        rewrite(root, "raw/review-timeline.json", doc)
        issues = validate_output_dir(root)
        assert any("单调不减" in i.message for i in _errors(issues))

    def test_pts_out_of_range_is_error(self, tmp_path: Path) -> None:
        root = copy_output_dir("output_full", tmp_path)
        doc = load_fixture("output_full", "raw/review-timeline.json")
        end = doc["analysis"]["analyzedRange"]["endMs"]
        doc["videoFrames"]["ptsMs"] = doc["videoFrames"]["ptsMs"] + [end + 10]
        doc["videoFrames"]["frameCount"] = len(doc["videoFrames"]["ptsMs"])
        rewrite(root, "raw/review-timeline.json", doc)
        issues = validate_output_dir(root)
        assert any("analyzedRange" in i.message for i in _errors(issues))

    def test_bin_count_mismatch_is_error(self, tmp_path: Path) -> None:
        root = copy_output_dir("output_full", tmp_path)
        doc = load_fixture("output_full", "raw/review-timeline.json")
        if doc["waveform"]["status"] != "complete":
            pytest.skip("夹具波形非 complete")
        doc["waveform"]["binCount"] = doc["waveform"]["binCount"] + 1
        rewrite(root, "raw/review-timeline.json", doc)
        issues = validate_output_dir(root)
        assert any("binCount" in i.message for i in _errors(issues))

    def test_peak_min_greater_than_max_is_error(self, tmp_path: Path) -> None:
        root = copy_output_dir("output_full", tmp_path)
        doc = load_fixture("output_full", "raw/review-timeline.json")
        if doc["waveform"]["status"] != "complete":
            pytest.skip("夹具波形非 complete")
        doc["waveform"]["peaks"][0] = [0.5, -0.5]
        rewrite(root, "raw/review-timeline.json", doc)
        issues = validate_output_dir(root)
        assert any("min <= max" in i.message for i in _errors(issues))

    def test_revision_mismatch_is_error(self, tmp_path: Path) -> None:
        root = copy_output_dir("output_full", tmp_path)
        doc = load_fixture("output_full", "raw/review-timeline.json")
        doc["sourceRevisionID"] = "ffffffffffff"
        rewrite(root, "raw/review-timeline.json", doc)
        issues = validate_output_dir(root)
        assert any(
            i.artifact == "review-timeline" and "不一致" in i.message
            for i in _errors(issues)
        )
