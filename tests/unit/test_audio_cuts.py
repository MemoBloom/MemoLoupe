"""media/audio_cuts.py 单元测试：六特征、novelty、峰值选择、边界分类与对齐。"""

from __future__ import annotations

import math

import numpy as np
import pytest

from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
from memoloupe.core.config import DEFAULT_CONFIG
from memoloupe.media.audio_cuts import (
    AUDIO_CUTS_VERSION,
    FEATURE_NAMES,
    FEATURE_WEIGHTS,
    MINIMUM_SCALES,
    classify_visual_boundary,
    compute_frame_features,
    detect_audio_cuts,
    novelty_scores,
    plan_boundary_alignment,
    select_peaks,
)

SR = 16000
FRAME_MS = 20
FRAME_LEN = SR * FRAME_MS // 1000  # 320


def _sine(freq: float, amplitude: float, n: int, *, sample_rate: int = SR) -> np.ndarray:
    t = np.arange(n) / sample_rate
    return np.round(amplitude * np.sin(2 * np.pi * freq * t)).astype(np.int16)


def test_constants_match_contract() -> None:
    assert AUDIO_CUTS_VERSION == "audio-cuts.v1"
    assert FEATURE_NAMES == (
        "rmsDb",
        "zeroCrossingRate",
        "roughness",
        "amplitudeShape",
        "autocorrelation1ms",
        "autocorrelation4ms",
    )
    assert MINIMUM_SCALES == (1.5, 0.015, 0.04, 0.025, 0.04, 0.04)
    assert FEATURE_WEIGHTS == (1.0, 0.8, 0.8, 0.6, 0.8, 0.8)


class TestComputeFrameFeatures:
    def test_frame_count_and_shape(self) -> None:
        samples = _sine(440.0, 16384, FRAME_LEN * 10 + 100)  # 尾巴不足一帧丢弃
        features = compute_frame_features(samples, sample_rate=SR, frame_ms=FRAME_MS)
        assert features.shape == (10, len(FEATURE_NAMES))

    def test_known_sine_values(self) -> None:
        features = compute_frame_features(
            _sine(440.0, 16384, FRAME_LEN), sample_rate=SR, frame_ms=FRAME_MS
        )
        row = dict(zip(FEATURE_NAMES, features[0]))
        # 半幅正弦 rms = 16384/√2 → -9.03 dB
        assert row["rmsDb"] == pytest.approx(-9.03, abs=0.05)
        # 440Hz：每 20ms 约 17.6 次过零 → 17.6/320
        assert row["zeroCrossingRate"] == pytest.approx(2 * 440 / SR, abs=0.005)
        # 帧内滞后自相关 ≈ cos(2π·f·lag)：1ms → cos(2.765) ≈ -0.93
        assert row["autocorrelation1ms"] == pytest.approx(-0.927, abs=0.02)
        # 4ms → cos(11.06 rad) ≈ 0.06
        assert row["autocorrelation4ms"] == pytest.approx(0.063, abs=0.02)
        # 稳态正弦峰均比 ≈ √2
        assert row["amplitudeShape"] == pytest.approx(math.sqrt(2), abs=0.02)

    def test_silence_no_nan(self) -> None:
        features = compute_frame_features(
            np.zeros(FRAME_LEN * 4, dtype=np.int16), sample_rate=SR, frame_ms=FRAME_MS
        )
        assert np.all(np.isfinite(features))
        row = dict(zip(FEATURE_NAMES, features[0]))
        assert row["rmsDb"] <= -90.0  # 静音地板
        assert row["zeroCrossingRate"] == 0.0

    def test_empty_input(self) -> None:
        features = compute_frame_features(
            np.zeros(100, dtype=np.int16), sample_rate=SR, frame_ms=FRAME_MS
        )
        assert features.shape == (0, len(FEATURE_NAMES))


class TestNoveltyScores:
    def test_constant_features_zero_novelty(self) -> None:
        features = np.tile(np.array([[-20.0, 0.05, 0.1, 1.4, 0.5, 0.3]]), (50, 1))
        scores, deltas = novelty_scores(features)
        assert scores.shape == (49,)
        assert deltas.shape == (49, 6)
        assert np.all(scores == 0.0)

    def test_single_step_produces_peak(self) -> None:
        # 中部 rmsDb 跳变 30 dB：30 / minScale(1.5) * weight(1.0) = 20
        features = np.tile(np.array([[-40.0, 0.05, 0.1, 1.4, 0.5, 0.3]]), (100, 1))
        features[50:, 0] = -10.0
        scores, deltas = novelty_scores(features)
        peak = int(np.argmax(scores))
        assert peak == 49  # pair 49 = frame 49 → 50 的变化
        assert scores[peak] == pytest.approx(20.0, abs=0.5)
        assert deltas[49][0] == pytest.approx(30.0)
        # 远离跳变处的 novelty 接近 0
        assert scores[10] == pytest.approx(0.0, abs=1e-9)

    def test_minimum_scales_floor(self) -> None:
        # 微小跳变 0.1 dB 应被 MINIMUM_SCALES 压低，不产生大 novelty
        features = np.tile(np.array([[-40.0, 0.05, 0.1, 1.4, 0.5, 0.3]]), (60, 1))
        features[30:, 0] = -39.9
        scores, _ = novelty_scores(features)
        assert scores[29] == pytest.approx(0.1 / 1.5 * 1.0, abs=0.02)


class TestSelectPeaks:
    def test_threshold_and_local_max(self) -> None:
        scores = np.zeros(50)
        scores[10] = 9.0  # > 8 且局部最大
        scores[30] = 7.9  # 低于阈值
        peaks = select_peaks(scores, frame_ms=20, threshold=8.0, min_separation_ms=250)
        assert [p["pairIndex"] for p in peaks] == [10]
        assert peaks[0]["score"] == pytest.approx(9.0)

    def test_min_separation_suppression_keeps_highest(self) -> None:
        scores = np.zeros(100)
        scores[40] = 10.0
        scores[45] = 12.0  # 相距 5 帧 = 100ms < 250ms，抑制低分者
        scores[80] = 9.0  # 相距足够远，保留
        peaks = select_peaks(scores, frame_ms=20, threshold=8.0, min_separation_ms=250)
        assert [p["pairIndex"] for p in peaks] == [45, 80]


class TestClassifyVisualBoundary:
    BOUNDARIES = [
        {"audioBoundaryID": "AU0001", "timeMs": 3200, "score": 12.4, "confidence": "high"},
        {"audioBoundaryID": "AU0002", "timeMs": 3450, "score": 9.5, "confidence": "medium"},
    ]

    def test_synchronized_cut(self) -> None:
        result = classify_visual_boundary(
            3203, self.BOUNDARIES, sync_tolerance_ms=100, association_window_ms=500
        )
        assert result["classification"] == "synchronizedCut"
        assert result["audioBoundaryID"] == "AU0001"
        assert result["audioTimeMs"] == 3200
        # offset 恒等式：offsetMs == audioTimeMs - visualTimeMs
        assert result["offsetMs"] == result["audioTimeMs"] - result["visualTimeMs"] == -3
        assert abs(result["offsetMs"]) <= 100
        assert result["audioBoundaryScore"] == pytest.approx(12.4)
        assert result["confidence"] == "high"
        assert "同步切" in result["labelZh"]

    def test_highest_score_in_window_then_offset_check(self) -> None:
        # 规约：窗内取 score 最高者（AU0001 @3200，12.4），再判 offset；
        # 偏差 -200ms 超出 syncTolerance，归为 audioBoundaryUndetermined。
        result = classify_visual_boundary(
            3400, self.BOUNDARIES, sync_tolerance_ms=100, association_window_ms=500
        )
        assert result["classification"] == "audioBoundaryUndetermined"
        assert result["audioBoundaryID"] == "AU0001"
        assert result["offsetMs"] == -200

    def test_picture_cut_audio_continuous(self) -> None:
        result = classify_visual_boundary(
            5000, self.BOUNDARIES, sync_tolerance_ms=100, association_window_ms=500
        )
        assert result["classification"] == "pictureCutAudioContinuous"
        assert "audioTimeMs" not in result

    def test_undetermined_when_beyond_sync_tolerance(self) -> None:
        result = classify_visual_boundary(
            3300, self.BOUNDARIES[:1],  # AU0001 @3200，偏差 -100 边界内
            sync_tolerance_ms=50,
            association_window_ms=500,
        )
        assert result["classification"] == "audioBoundaryUndetermined"
        assert result["audioBoundaryID"] == "AU0001"
        assert result["offsetMs"] == -100
        assert result["confidence"] == "low"


def _shots_dict(times: list[int]) -> dict:
    """times 为边界点序列，构造相邻连续的 shots dict。"""
    shots = []
    for i, (start, end) in enumerate(zip(times, times[1:]), start=1):
        shots.append(
            {
                "shotID": f"SH{i:04d}",
                "sequenceIndex": i,
                "detectedStartMs": start,
                "detectedEndMs": end,
                "finalStartMs": start,
                "finalEndMs": end,
                "durationMs": end - start,
            }
        )
    return {"analysis": {"fps": 2.0}, "boundaries": [], "shots": shots}


class TestPlanBoundaryAlignment:
    def test_high_confidence_sync_cut_moved(self) -> None:
        shots = _shots_dict([0, 10000, 20000])["shots"]
        internal = [
            {
                "visualTimeMs": 10000,
                "classification": "synchronizedCut",
                "confidence": "high",
                "audioTimeMs": 10040,
                "offsetMs": 40,
                "audioBoundaryID": "AU0001",
            }
        ]
        moved = plan_boundary_alignment(internal, shots, min_shot_ms=4000)
        assert len(moved) == 1
        assert moved[0]["audioTimeMs"] == 10040
        assert moved[0]["leftShotID"] == "SH0001"
        assert moved[0]["rightShotID"] == "SH0002"

    def test_medium_confidence_not_moved(self) -> None:
        shots = _shots_dict([0, 10000, 20000])["shots"]
        internal = [
            {
                "visualTimeMs": 10000,
                "classification": "synchronizedCut",
                "confidence": "medium",
                "audioTimeMs": 10040,
                "offsetMs": 40,
                "audioBoundaryID": "AU0001",
            }
        ]
        assert plan_boundary_alignment(internal, shots, min_shot_ms=4000) == []

    def test_min_shot_length_violation_not_moved(self) -> None:
        # 镜头仅 4.5s，最小 4s：移到 3900ms 会让左镜头只剩 3.9s，不动
        shots = _shots_dict([0, 4500, 9000])["shots"]
        internal = [
            {
                "visualTimeMs": 4500,
                "classification": "synchronizedCut",
                "confidence": "high",
                "audioTimeMs": 3900,
                "offsetMs": -600,
                "audioBoundaryID": "AU0001",
            }
        ]
        assert plan_boundary_alignment(internal, shots, min_shot_ms=4000) == []

    def test_non_sync_classification_not_moved(self) -> None:
        shots = _shots_dict([0, 10000, 20000])["shots"]
        internal = [
            {
                "visualTimeMs": 10000,
                "classification": "pictureCutAudioContinuous",
                "confidence": "medium",
            }
        ]
        assert plan_boundary_alignment(internal, shots, min_shot_ms=4000) == []


class TestDetectAudioCutsUnavailable:
    def test_no_audio_track(self, tmp_path) -> None:
        shots = _shots_dict([0, 2000, 4000])
        media = {
            "source": {
                "audioTracks": [],
                "analyzedRange": {"startMs": 0, "endMs": 4000},
            }
        }
        result = detect_audio_cuts(tmp_path / "x.mp4", shots, media, DEFAULT_CONFIG)
        validate_artifact(ArtifactName.AUDIO_CUTS, result)
        assert result["status"] == "unavailable"
        assert result["boundaries"] == []
        entries = result["shots"]
        assert len(entries) == 2
        assert entries[0]["boundaryIn"]["classification"] == "sourceStart"
        assert entries[0]["boundaryOut"]["classification"] == "unavailable"
        assert entries[1]["boundaryIn"]["classification"] == "unavailable"
        assert entries[1]["boundaryOut"]["classification"] == "sourceEnd"
        assert result["movedBoundaries"] == []
