"""契约测试：output_full 通过跨文件校验，派生破坏必须被对应检查捕获。"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import copy_output_dir, load_fixture, mutate, rewrite

from memoloupe.validate.cross_artifact import validate_output_dir
from memoloupe.validate.json_contracts import ValidationIssue


def _errors(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    return [i for i in issues if i.severity == "error"]


def _warnings(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    return [i for i in issues if i.severity == "warning"]


class TestCleanFixtures:
    @pytest.mark.parametrize("strict", [False, True])
    def test_output_full_zero_issues(self, strict: bool) -> None:
        issues = validate_output_dir(Path("tests/fixtures/output_full"), strict=strict)
        assert issues == [], "\n".join(str(i) for i in issues)

    @pytest.mark.parametrize("strict", [False, True])
    def test_minimal_zero_errors(self, strict: bool) -> None:
        issues = validate_output_dir(Path("tests/fixtures/minimal"), strict=strict)
        assert _errors(issues) == [], "\n".join(str(i) for i in _errors(issues))

    def test_missing_file_is_warning_not_error(self, tmp_path: Path) -> None:
        root = copy_output_dir("output_full", tmp_path)
        (root / "raw" / "camera-motion.json").unlink()
        issues = validate_output_dir(root)
        assert _errors(issues) == []
        assert any(
            i.artifact == "camera-motion" and "缺失" in i.message
            for i in _warnings(issues)
        )


class TestDerivedBreakage:
    """每个用例复制 output_full 后破坏一个文件，断言对应检查报错。"""

    def _run(self, tmp_path: Path, rel: str, broken: dict) -> list[ValidationIssue]:
        root = copy_output_dir("output_full", tmp_path)
        rewrite(root, rel, broken)
        return validate_output_dir(root)

    def test_unknown_shot_reference(self, tmp_path: Path) -> None:
        data = load_fixture("output_full", "raw/audio-cuts.json")
        broken = mutate(data, "shots[0].shotID", "SH0099")
        issues = self._run(tmp_path, "raw/audio-cuts.json", broken)
        assert any(
            i.artifact == "audio-cuts" and "不存在于 shots.json" in i.message
            for i in _errors(issues)
        )

    def test_revision_mismatch(self, tmp_path: Path) -> None:
        data = load_fixture("output_full", "raw/frame-evidence.json")
        broken = mutate(data, "request.sourceRevisionID", "ffffffffffff")
        issues = self._run(tmp_path, "raw/frame-evidence.json", broken)
        assert any(
            i.artifact == "frame-evidence" and "revisionID 不一致" in i.message
            for i in _errors(issues)
        )

    def test_shot_range_overlap(self, tmp_path: Path) -> None:
        data = load_fixture("output_full", "raw/shots.json")
        broken = mutate(data, "shots[1].finalStartMs", 3200)
        broken = mutate(broken, "shots[1].durationMs", 3200)
        issues = self._run(tmp_path, "raw/shots.json", broken)
        assert any(
            i.artifact == "shots" and "重叠" in i.message for i in _errors(issues)
        )

    def test_duration_identity_broken(self, tmp_path: Path) -> None:
        data = load_fixture("output_full", "raw/shots.json")
        broken = mutate(data, "shots[0].durationMs", 3000)
        issues = self._run(tmp_path, "raw/shots.json", broken)
        assert any(
            i.artifact == "shots" and "finalEndMs - finalStartMs" in i.message
            for i in _errors(issues)
        )

    def test_audio_offset_identity_broken(self, tmp_path: Path) -> None:
        data = load_fixture("output_full", "raw/audio-cuts.json")
        # offsetMs 改成 -7（仍在容差内，但违反 offsetMs == audioTimeMs - visualTimeMs）
        broken = mutate(data, "shots[0].boundaryOut.offsetMs", -7)
        issues = self._run(tmp_path, "raw/audio-cuts.json", broken)
        assert any(
            i.artifact == "audio-cuts" and "audioTimeMs - visualTimeMs" in i.message
            for i in _errors(issues)
        )

    def test_sync_tolerance_exceeded(self, tmp_path: Path) -> None:
        data = load_fixture("output_full", "raw/audio-cuts.json")
        # audioTimeMs 改为 3000：恒等式 offset=-203 成立，但超出 syncToleranceMs=100
        broken = mutate(data, "shots[0].boundaryOut.audioTimeMs", 3000)
        broken = mutate(broken, "shots[0].boundaryOut.offsetMs", -203)
        issues = self._run(tmp_path, "raw/audio-cuts.json", broken)
        assert any(
            i.artifact == "audio-cuts" and "syncToleranceMs" in i.message
            for i in _errors(issues)
        )

    def test_music_state_tally_mismatch(self, tmp_path: Path) -> None:
        data = load_fixture("output_full", "raw/music-flags.json")
        broken = mutate(data, "stateTally.music", 2)
        issues = self._run(tmp_path, "raw/music-flags.json", broken)
        assert any(
            i.artifact == "music-flags" and "stateTally" in i.message
            for i in _errors(issues)
        )

    def test_unified_media_missing_response_shot(self, tmp_path: Path) -> None:
        data = load_fixture("output_full", "raw/unified-media.json")
        broken = mutate(
            data,
            "batches[0].response.shots",
            data["batches"][0]["response"]["shots"][:2],
        )
        issues = self._run(tmp_path, "raw/unified-media.json", broken)
        assert any(
            i.artifact == "unified-media" and "集合不一致" in i.message
            for i in _errors(issues)
        )

    def test_unified_media_failed_batch_without_response_is_legal(
        self, tmp_path: Path
    ) -> None:
        """partial 允许成功与永久失败并存：failed 批次可无 response（docs/02 §4.7）。"""
        data = load_fixture("output_full", "raw/unified-media.json")
        batch = data["batches"][0]
        shots = batch["response"]["shots"]
        failed_shot = shots[-1]["shotID"]
        batch["shotIDs"] = [s["shotID"] for s in shots[:-1]]
        batch["response"]["shots"] = shots[:-1]
        data["batches"].append(
            {"batchID": "B0002", "shotIDs": [failed_shot], "status": "failed"}
        )
        data["shotStatuses"][failed_shot] = "permanent_failure"
        data["completedShots"] = len(shots) - 1
        data["permanentFailureShots"] = 1
        data["status"] = "partial"
        issues = self._run(tmp_path, "raw/unified-media.json", data)
        assert not [
            i
            for i in _errors(issues)
            if i.artifact == "unified-media" and "集合不一致" in i.message
        ]

    def test_story_block_unknown_shot(self, tmp_path: Path) -> None:
        data = load_fixture("output_full", "raw/story-blocks.json")
        broken = mutate(data, "blocks[0].shotIDs", ["SH0001", "SH0099"])
        issues = self._run(tmp_path, "raw/story-blocks.json", broken)
        assert any(
            i.artifact == "story-blocks" and "不存在于 shots.json" in i.message
            for i in _errors(issues)
        )

    def test_profile_slot_misaligned(self, tmp_path: Path) -> None:
        data = load_fixture("output_full", "style-profile.json")
        broken = mutate(data, "structure.slots[0].slotId", "S009")
        issues = self._run(tmp_path, "style-profile.json", broken)
        assert any(
            i.artifact == "style-profile" and "不对齐" in i.message
            for i in _errors(issues)
        )

    def test_duration_share_sum_broken(self, tmp_path: Path) -> None:
        data = load_fixture("output_full", "style-profile.json")
        broken = mutate(data, "structure.slots[0].L1.durationShare", 0.5)
        issues = self._run(tmp_path, "style-profile.json", broken)
        assert any(
            i.artifact == "style-profile" and "durationShare 之和" in i.message
            for i in _errors(issues)
        )

    def test_avg_shot_seconds_broken(self, tmp_path: Path) -> None:
        data = load_fixture("output_full", "style-profile.json")
        broken = mutate(data, "structure.slots[0].L3.avgShotSeconds", 9.9)
        issues = self._run(tmp_path, "style-profile.json", broken)
        assert any(
            i.artifact == "style-profile" and "avgShotSeconds" in i.message
            for i in _errors(issues)
        )

    def test_broken_evidence_ref_file(self, tmp_path: Path) -> None:
        data = load_fixture("output_full", "raw/unified-media.json")
        broken = mutate(
            data,
            "batches[0].response.shots[0].evidenceRefs",
            ["evidence/frames/NOPE.jpg"],
        )
        issues = self._run(tmp_path, "raw/unified-media.json", broken)
        assert any(
            i.artifact == "unified-media" and "文件不存在" in i.message
            for i in _errors(issues)
        )

    def test_broken_evidence_ref_pointer(self, tmp_path: Path) -> None:
        data = load_fixture("output_full", "raw/unified-media.json")
        broken = mutate(
            data,
            "batches[0].response.shots[0].evidenceRefs",
            ["raw/shots.json#shots[9]"],
        )
        issues = self._run(tmp_path, "raw/unified-media.json", broken)
        assert any(
            i.artifact == "unified-media" and "指针不可解析" in i.message
            for i in _errors(issues)
        )

    def test_frame_fileref_missing(self, tmp_path: Path) -> None:
        root = copy_output_dir("output_full", tmp_path)
        (root / "evidence" / "frames" / "F_SH0002_MAIN.jpg").unlink()
        issues = validate_output_dir(root)
        assert any(
            i.artifact == "frame-evidence" and "fileRef" in i.message
            for i in _errors(issues)
        )


class TestStrictCoverage:
    """complete 的镜头级文件缺镜头：非 strict 记 warning，strict 记 error。"""

    def _broken_root(self, tmp_path: Path) -> Path:
        root = copy_output_dir("output_full", tmp_path)
        data = load_fixture("output_full", "raw/camera-motion.json")
        broken = mutate(data, "shots", data["shots"][:2])
        rewrite(root, "raw/camera-motion.json", broken)
        return root

    def test_non_strict_warns(self, tmp_path: Path) -> None:
        issues = validate_output_dir(self._broken_root(tmp_path))
        assert _errors(issues) == []
        assert any(
            i.artifact == "camera-motion" and "未覆盖" in i.message
            for i in _warnings(issues)
        )

    def test_strict_errors(self, tmp_path: Path) -> None:
        issues = validate_output_dir(self._broken_root(tmp_path), strict=True)
        assert any(
            i.artifact == "camera-motion" and "未覆盖" in i.message
            for i in _errors(issues)
        )
