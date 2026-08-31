"""media/shots v2 纯函数单元测试。"""

from __future__ import annotations

import pytest

from memoloupe.core.config import DEFAULT_CONFIG
from memoloupe.media.shots import (
    SHOT_DETECTION_VERSION,
    frame_features,
    pair_metrics,
    select_boundaries,
)

_CFG = DEFAULT_CONFIG["shots"]
_W = _H = 16


def _solid(rgb: tuple[int, int, int], w: int = _W, h: int = _H) -> bytes:
    return bytes(rgb) * (w * h)


def _texture(seed: int, w: int = _W, h: int = _H) -> bytes:
    values: list[int] = []
    for y in range(h):
        for x in range(w):
            base = (x * 73 + y * 151 + seed * 31) % 180
            values.extend(((base + 20) % 256, (base * 3 + 40) % 256, (base * 7 + 60) % 256))
    return bytes(values)


def _metrics(a: bytes, b: bytes) -> dict:
    return pair_metrics(
        frame_features(a, _W, _H),
        frame_features(b, _W, _H),
        edge_weight=_CFG["edgeWeight"],
    )


class TestFrameFeatures:
    def test_solid_red_frame(self):
        features = frame_features(_solid((255, 0, 0)), _W, _H)
        assert features.hue.mean() == pytest.approx(0.0)
        assert features.saturation.mean() == pytest.approx(1.0)
        assert features.value.mean() == pytest.approx(1.0)
        assert features.edges.max() == pytest.approx(0.0)
        assert features.brightness == pytest.approx(1.0)

    def test_rejects_wrong_rgb_size(self):
        with pytest.raises(ValueError):
            frame_features(b"\x00" * 10, _W, _H)


class TestPairMetrics:
    def test_identical_frames_have_zero_change(self):
        metrics = _metrics(_texture(1), _texture(1))
        assert metrics["histogramSimilarity"] == pytest.approx(1.0)
        assert metrics["edgeSimilarity"] == pytest.approx(1.0)
        assert metrics["changeValue"] == pytest.approx(0.0)
        assert metrics["score"] == pytest.approx(0.0)

    def test_color_cut_scores_lower_than_mild_change(self):
        cut = _metrics(_solid((255, 0, 0)), _solid((0, 0, 255)))
        mild = _metrics(_solid((128, 128, 128)), _solid((130, 130, 130)))
        assert cut["contentDelta"] > _CFG["minContentValue"]
        assert cut["score"] < mild["score"]
        assert 0.0 <= cut["histogramSimilarity"] <= 1.0

    def test_sobel_detects_structural_change(self):
        flat = _solid((0, 0, 0))
        checker = bytes(
            channel
            for y in range(_H)
            for x in range(_W)
            for channel in ((255, 255, 255) if (x + y) % 2 else (0, 0, 0))
        )
        metrics = _metrics(flat, checker)
        assert metrics["edgeDelta"] > 0
        assert metrics["edgeSimilarity"] < 1


class TestSelectBoundaries:
    def test_hard_cut_selected_high_confidence(self):
        changes = [2.0] * 20
        changes[9] = 65.0
        selected = select_boundaries(changes, minimum_frames=8)
        assert [item["pairIndex"] for item in selected] == [9]
        assert selected[0]["selectionReason"] == "rawNegativeScore"
        assert selected[0]["confidence"] == "high"
        assert selected[0]["score"] == pytest.approx(-65.0)

    def test_local_adaptive_outlier_selected(self):
        changes = [2.0] * 20
        changes[9] = 20.0
        selected = select_boundaries(changes, minimum_frames=8)
        assert [item["pairIndex"] for item in selected] == [9]
        assert selected[0]["selectionReason"] == "adaptiveOutlier"
        assert selected[0]["adaptiveRatio"] == pytest.approx(10.0)

    def test_global_fast_motion_without_local_outlier_is_not_selected(self):
        changes = [20.0] * 20
        assert select_boundaries(changes, minimum_frames=8) == []

    def test_short_segment_merge_preserves_suppressed_evidence(self):
        changes = [2.0] * 20
        changes[6] = 20.0
        changes[9] = 25.0
        selected, suppressed = select_boundaries(
            changes, minimum_frames=8, adaptive_window=2, return_suppressed=True
        )
        assert [item["pairIndex"] for item in selected] == [9]
        assert [item["pairIndex"] for item in suppressed] == [6]
        assert suppressed[0]["suppressionReason"] == "shortSegmentMerge"
        assert suppressed[0]["mergedIntoPairIndex"] == 9

    def test_two_strong_rapid_cuts_are_preserved(self):
        changes = [2.0] * 20
        changes[5] = 65.0
        changes[9] = 70.0
        selected = select_boundaries(
            changes, minimum_frames=10, rapid_cut_minimum_frames=3
        )
        assert [item["pairIndex"] for item in selected] == [5, 9]

    def test_ssim_rejects_ambiguous_adaptive_candidate(self):
        changes = [2.0] * 20
        changes[9] = 20.0
        ssim = [None] * 20
        ssim[9] = 0.98
        selected, suppressed = select_boundaries(
            changes,
            minimum_frames=8,
            ssim_values=ssim,
            return_suppressed=True,
        )
        assert selected == []
        assert suppressed[0]["suppressionReason"] == "ssimSimilarityRejected"

    def test_first_and_last_pairs_are_not_candidates(self):
        changes = [99.0] + [2.0] * 17 + [99.0]
        assert select_boundaries(changes, minimum_frames=8) == []

    def test_short_sequence_yields_no_candidates(self):
        assert select_boundaries([55.0], minimum_frames=8) == []


def test_algorithm_version_constant():
    assert SHOT_DETECTION_VERSION == "shots.v2"
