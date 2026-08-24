"""ids 模块单元测试。"""

from __future__ import annotations

import pytest

from memoloupe.core import ids


class TestShotId:
    def test_valid(self) -> None:
        assert ids.is_valid_shot_id("SH0001")
        assert ids.is_valid_shot_id("SH9999")

    @pytest.mark.parametrize(
        "value",
        ["SH001", "SH00001", "sh0001", "SH00A1", "SH", "", "AU0001", 1, None],
    )
    def test_invalid(self, value: object) -> None:
        assert not ids.is_valid_shot_id(value)

    def test_make(self) -> None:
        assert ids.make_shot_id(1) == "SH0001"
        assert ids.make_shot_id(42) == "SH0042"

    def test_validate(self) -> None:
        assert ids.validate_shot_id("SH0001") == "SH0001"
        with pytest.raises(ValueError):
            ids.validate_shot_id("XX0001")


class TestAudioBoundaryId:
    def test_valid(self) -> None:
        assert ids.is_valid_audio_boundary_id("AU0001")

    @pytest.mark.parametrize("value", ["AU001", "AU00001", "SH0001", "au0001", ""])
    def test_invalid(self, value: object) -> None:
        assert not ids.is_valid_audio_boundary_id(value)

    def test_make_and_validate(self) -> None:
        assert ids.make_audio_boundary_id(7) == "AU0007"
        assert ids.validate_audio_boundary_id("AU0007") == "AU0007"
        with pytest.raises(ValueError):
            ids.validate_audio_boundary_id("SH0001")


class TestFrameEvidenceId:
    def test_valid_main(self) -> None:
        assert ids.is_valid_frame_evidence_id("F_SH0001_MAIN")

    def test_valid_keyframe(self) -> None:
        assert ids.is_valid_frame_evidence_id("F_SH0001_K01")
        assert ids.is_valid_frame_evidence_id("F_SH9999_K99")

    @pytest.mark.parametrize(
        "value",
        [
            "F_SH0001",  # 缺少后缀
            "F_SH0001_K1",  # 下标必须两位
            "F_SH0001_K001",
            "F_SH001_MAIN",  # 镜头 ID 不合法
            "F_SH0001_main",
            "F_AU0001_MAIN",
            "SH0001_MAIN",
        ],
    )
    def test_invalid(self, value: object) -> None:
        assert not ids.is_valid_frame_evidence_id(value)

    def test_make_main(self) -> None:
        assert ids.make_main_frame_evidence_id("SH0001") == "F_SH0001_MAIN"

    def test_make_keyframe(self) -> None:
        assert ids.make_keyframe_evidence_id("SH0001", 1) == "F_SH0001_K01"
        assert ids.make_keyframe_evidence_id("SH0001", 12) == "F_SH0001_K12"

    def test_make_rejects_bad_shot_id(self) -> None:
        with pytest.raises(ValueError):
            ids.make_main_frame_evidence_id("XX0001")

    def test_make_keyframe_rejects_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            ids.make_keyframe_evidence_id("SH0001", 0)
        with pytest.raises(ValueError):
            ids.make_keyframe_evidence_id("SH0001", 100)

    def test_validate(self) -> None:
        assert ids.validate_frame_evidence_id("F_SH0001_MAIN") == "F_SH0001_MAIN"
        with pytest.raises(ValueError):
            ids.validate_frame_evidence_id("F_SH0001")


class TestStoryIds:
    def test_block(self) -> None:
        assert ids.is_valid_story_block_id("B0001")
        assert not ids.is_valid_story_block_id("B001")
        assert not ids.is_valid_story_block_id("S001")
        assert ids.make_story_block_id(3) == "B0003"
        assert ids.validate_story_block_id("B0003") == "B0003"
        with pytest.raises(ValueError):
            ids.validate_story_block_id("B003")

    def test_slot(self) -> None:
        assert ids.is_valid_story_slot_id("S001")
        assert ids.is_valid_story_slot_id("S999")
        assert not ids.is_valid_story_slot_id("S0001")
        assert not ids.is_valid_story_slot_id("S01")
        assert ids.make_story_slot_id(5) == "S005"
        assert ids.validate_story_slot_id("S005") == "S005"
        with pytest.raises(ValueError):
            ids.validate_story_slot_id("B0001")

    def test_namespace_not_inferable(self) -> None:
        # B0001 与 S001 等不同命名空间不得被互相接受
        assert not ids.is_valid_story_slot_id("B0001")
        assert not ids.is_valid_shot_id("B0001")
