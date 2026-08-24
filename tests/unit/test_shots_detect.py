"""media/shots 纯函数单元测试（合成内存帧，不跑 ffmpeg）。"""

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


def _solid(value: int, w: int = _W, h: int = _H) -> bytes:
    return bytes([value]) * (w * h)


def _texture(seed: int, w: int = _W, h: int = _H, base: int = 60, span: int = 120) -> bytes:
    """确定性伪随机纹理帧，值落在 [base, base+span)。"""
    return bytes(
        base + (x * 73 + y * 151 + seed * 31) % span
        for y in range(h)
        for x in range(w)
    )


def _shift(frame: bytes, delta: int) -> bytes:
    return bytes(min(255, b + delta) for b in frame)


def _metrics(a: bytes, b: bytes) -> dict:
    fa = frame_features(a, _W, _H, bins=_CFG["histogramBins"])
    fb = frame_features(b, _W, _H, bins=_CFG["histogramBins"])
    return pair_metrics(
        fa,
        fb,
        histogram_weight=_CFG["histogramWeight"],
        edge_weight=_CFG["edgeWeight"],
        score_offset=_CFG["scoreOffset"],
    )


def _scores_of(frames: list[bytes]) -> list[float]:
    return [_metrics(a, b)["score"] for a, b in zip(frames, frames[1:])]


class TestFrameFeatures:
    def test_solid_frame(self):
        f = frame_features(_solid(128), _W, _H, bins=254)
        assert len(f.histogram) == 254
        assert sum(f.histogram) == pytest.approx(1.0)
        assert sum(1 for c in f.histogram if c > 0) == 1
        assert f.histogram[128 * 254 // 256] == pytest.approx(1.0)
        assert f.edge_density == 0.0
        assert f.brightness == pytest.approx(128 / 255)

    def test_rejects_wrong_size(self):
        with pytest.raises(ValueError):
            frame_features(b"\x00" * 10, _W, _H)


class TestPairMetrics:
    def test_identical_frames_score_is_offset(self):
        m = _metrics(_texture(1), _texture(1))
        assert m["histogramSimilarity"] == pytest.approx(1.0)
        assert m["edgeSimilarity"] == pytest.approx(1.0)
        assert m["brightnessDelta"] == pytest.approx(0.0)
        assert m["score"] == pytest.approx(_CFG["scoreOffset"])

    def test_direction_hard_cut_lower_than_mild_change(self):
        base = _texture(1)
        hard = _metrics(base, _solid(120))
        mild = _metrics(base, _shift(base, 1))
        # 方向不变量：越低越像硬切
        assert hard["score"] < mild["score"]
        assert hard["score"] < 0 < mild["score"]
        assert 0.0 <= hard["histogramSimilarity"] <= 1.0
        assert 0.0 <= hard["edgeSimilarity"] <= 1.0

    def test_solid_to_solid_cut_scores_positive(self):
        # 锁定公式行为：两个无边缘纯色帧互切时 edgeSimilarity=1，
        # score = offset - histogramWeight > 0，只能靠 adaptiveOutlier 入选。
        m = _metrics(_solid(200), _solid(20))
        assert m["histogramSimilarity"] == pytest.approx(0.0)
        assert m["edgeSimilarity"] == pytest.approx(1.0)
        assert m["score"] == pytest.approx(
            _CFG["scoreOffset"] - _CFG["histogramWeight"]
        )
        assert m["score"] > 0


class TestSelectBoundaries:
    def test_hard_cut_selected_raw_negative_high_confidence(self):
        frames = [_texture(1)] * 6 + [_solid(120)] * 6
        scores = _scores_of(frames)
        cut_pair = 5  # frames[5]→frames[6] 之间突变
        assert scores[cut_pair] == min(scores)
        assert scores[cut_pair] < -2  # high confidence 区间
        selected = select_boundaries(scores, minimum_frames=4, mad_k=3.0)
        assert [c["pairIndex"] for c in selected] == [cut_pair]
        assert selected[0]["selectionReason"] == "rawNegativeScore"
        assert selected[0]["confidence"] == "high"

    def test_solid_to_solid_cut_selected_via_adaptive_outlier(self):
        frames = [_solid(200)] * 10 + [_solid(20)] * 10
        scores = _scores_of(frames)
        cut_pair = 9
        assert scores[cut_pair] > 0  # 不走 rawNegativeScore
        selected = select_boundaries(scores, minimum_frames=8, mad_k=3.0)
        assert [c["pairIndex"] for c in selected] == [cut_pair]
        assert selected[0]["selectionReason"] == "adaptiveOutlier"
        assert selected[0]["confidence"] == "low"  # score >= 0

    def test_slow_gradient_not_detected(self):
        base = _texture(1)
        frames = [_shift(base, k) for k in range(12)]
        scores = _scores_of(frames)
        assert all(s > 0 for s in scores)
        assert select_boundaries(scores, minimum_frames=4, mad_k=3.0) == []

    def test_minimum_frames_dedup_keeps_lowest_score(self):
        scores = [5.5] * 20
        scores[3] = -1.0
        scores[5] = -2.0  # 与 3 相距 2 < 8，只保留最低分
        scores[14] = -1.5  # 与 5 相距 9 >= 8，保留
        selected = select_boundaries(scores, minimum_frames=8, mad_k=3.0)
        assert [c["pairIndex"] for c in selected] == [5, 14]

    def test_adaptive_outlier_path_synthetic(self):
        scores = [5.0] * 19
        scores[9] = 2.0  # > 0 但显著低于稳健分布
        selected = select_boundaries(scores, minimum_frames=8, mad_k=3.0)
        assert [c["pairIndex"] for c in selected] == [9]
        assert selected[0]["selectionReason"] == "adaptiveOutlier"
        assert selected[0]["confidence"] == "low"

    def test_first_and_last_pair_not_candidates(self):
        scores = [-9.0] + [5.0] * 17 + [-9.0]
        assert select_boundaries(scores, minimum_frames=8, mad_k=3.0) == []

    def test_short_sequence_yields_no_candidates(self):
        scores = [-5.0, 5.0, -5.0]  # 4 帧 < minimumFrames=8
        assert select_boundaries(scores, minimum_frames=8, mad_k=3.0) == []

    def test_confidence_bands(self):
        scores = [5.5] * 20
        scores[4] = -2.5  # high
        scores[12] = -0.5  # medium
        selected = select_boundaries(scores, minimum_frames=8, mad_k=3.0)
        by_pair = {c["pairIndex"]: c for c in selected}
        assert by_pair[4]["confidence"] == "high"
        assert by_pair[12]["confidence"] == "medium"


def test_algorithm_version_constant():
    assert SHOT_DETECTION_VERSION == "shots.v1"
