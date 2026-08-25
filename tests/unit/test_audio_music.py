"""media/audio_music.py 单元测试：STFT 特征、语音间隙、gap 判定、镜头聚合与降级。"""

from __future__ import annotations

import numpy as np
import pytest

from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
from memoloupe.core.config import DEFAULT_CONFIG
from memoloupe.media.audio_music import (
    AUDIO_MUSIC_VERSION,
    aggregate_shot_states,
    classify_gap,
    compute_overlap_ratio,
    detect_music,
    detect_texture_events,
    find_speech_gaps,
    stft_features,
)

SR = 22050


def _sine(freq: float, amplitude: float, seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return np.round(amplitude * 32767 * np.sin(2 * np.pi * freq * t)).astype(np.int16)


def test_version_constant() -> None:
    assert AUDIO_MUSIC_VERSION == "music.v1"


class TestStftFeatures:
    def test_tonal_low_flatness_high_bass(self) -> None:
        # 响亮 220Hz 正弦：低谱平坦度、低频能量集中、电平约 -9dB
        samples = _sine(220.0, 0.5, 1.0)
        feats = stft_features(samples, sample_rate=SR, window=2048, hop=512)
        assert feats["levelDb"].shape == feats["flatness"].shape == feats["bassEnergy"].shape
        assert len(feats["timesSec"]) == len(feats["levelDb"])
        mid = len(feats["levelDb"]) // 2
        assert float(np.median(feats["levelDb"])) == pytest.approx(-9.0, abs=1.0)
        assert float(np.median(feats["flatness"])) < 0.05
        assert float(np.median(feats["bassEnergy"])) > 150.0

    def test_high_freq_sine_low_bass(self) -> None:
        # 5kHz 正弦：响亮但低频带能量低
        samples = _sine(5000.0, 0.5, 1.0)
        feats = stft_features(samples, sample_rate=SR, window=2048, hop=512)
        assert float(np.median(feats["levelDb"])) == pytest.approx(-9.0, abs=1.0)
        assert float(np.median(feats["bassEnergy"])) < 10.0

    def test_noise_high_flatness(self) -> None:
        rng = np.random.default_rng(42)
        samples = np.round(0.05 * 32767 * rng.standard_normal(SR)).astype(np.int16)
        feats = stft_features(samples, sample_rate=SR, window=2048, hop=512)
        assert float(np.median(feats["flatness"])) > 0.3

    def test_silence_floor_and_finite(self) -> None:
        feats = stft_features(np.zeros(SR, dtype=np.int16), sample_rate=SR, window=2048, hop=512)
        assert np.all(np.isfinite(feats["levelDb"]))
        assert np.all(np.isfinite(feats["flatness"]))
        assert float(np.max(feats["levelDb"])) <= -90.0
        assert float(np.max(feats["bassEnergy"])) < 1.0

    def test_short_input_empty(self) -> None:
        feats = stft_features(np.zeros(1000, dtype=np.int16), sample_rate=SR, window=2048, hop=512)
        assert len(feats["levelDb"]) == 0


class TestFindSpeechGaps:
    def test_leading_internal_trailing(self) -> None:
        segments = [
            {"startMs": 1000, "endMs": 2000},
            {"startMs": 3000, "endMs": 5000},
        ]
        gaps = find_speech_gaps(segments, range_start_ms=0, range_end_ms=6000)
        assert gaps == [(0, 1000), (2000, 3000), (5000, 6000)]

    def test_min_gap_filter(self) -> None:
        segments = [
            {"startMs": 1000, "endMs": 2000},
            {"startMs": 2200, "endMs": 3000},  # 200ms 间隙 < 300ms
        ]
        gaps = find_speech_gaps(segments, range_start_ms=0, range_end_ms=6000, min_gap_ms=300)
        assert (2000, 2200) not in gaps
        assert (0, 1000) in gaps
        assert (3000, 6000) in gaps

    def test_empty_segments_whole_range(self) -> None:
        assert find_speech_gaps([], range_start_ms=0, range_end_ms=4000) == [(0, 4000)]

    def test_touching_segments_no_gap(self) -> None:
        segments = [{"startMs": 0, "endMs": 2000}, {"startMs": 2000, "endMs": 4000}]
        assert find_speech_gaps(segments, range_start_ms=0, range_end_ms=4000) == []


class TestClassifyGap:
    THRESHOLDS = {"musicLevelDb": -18.0, "musicBassEnergy": 150.0, "silentLevelDb": -22.0}

    def test_music_by_level(self) -> None:
        assert classify_gap(-12.0, 10.0, self.THRESHOLDS) == "music"

    def test_music_by_bass(self) -> None:
        assert classify_gap(-20.0, 200.0, self.THRESHOLDS) == "music"

    def test_silent(self) -> None:
        assert classify_gap(-30.0, 1.0, self.THRESHOLDS) == "silent"

    def test_between_is_unknown(self) -> None:
        assert classify_gap(-20.0, 100.0, self.THRESHOLDS) == "unknown"

    def test_boundary_inclusive(self) -> None:
        # level >= musicLevelDb → music；level <= silentLevelDb → silent
        assert classify_gap(-18.0, 0.0, self.THRESHOLDS) == "music"
        assert classify_gap(-22.0, 0.0, self.THRESHOLDS) == "silent"


class TestTextureEvents:
    def test_rise_and_fall(self) -> None:
        times = [i * 0.023 for i in range(200)]
        flatness = [0.05] * 100 + [0.35] * 100
        events = detect_texture_events(
            times, flatness, delta_threshold=0.1, min_separation_ms=250
        )
        assert len(events) == 1
        assert events[0]["kind"] == "textureRise"
        assert events[0]["flatnessDelta"] == pytest.approx(0.30, abs=0.01)
        assert events[0]["atSec"] == pytest.approx(times[100], abs=0.05)
        assert "粗糙" in events[0]["label"]

    def test_fall_label(self) -> None:
        times = [i * 0.023 for i in range(200)]
        flatness = [0.35] * 100 + [0.05] * 100
        events = detect_texture_events(
            times, flatness, delta_threshold=0.1, min_separation_ms=250
        )
        assert events[0]["kind"] == "textureFall"
        assert events[0]["flatnessDelta"] == pytest.approx(-0.30, abs=0.01)
        assert "平滑" in events[0]["label"]

    def test_below_threshold_no_events(self) -> None:
        times = [i * 0.023 for i in range(100)]
        flatness = [0.1 + 0.05 * ((i % 5) / 5) for i in range(100)]
        events = detect_texture_events(
            times, flatness, delta_threshold=0.1, min_separation_ms=250
        )
        assert events == []


class TestOverlapRatio:
    def test_full_cover(self) -> None:
        assert compute_overlap_ratio([(0, 4000)], 0, 4000) == pytest.approx(1.0)

    def test_partial(self) -> None:
        assert compute_overlap_ratio([(1000, 3000)], 0, 4000) == pytest.approx(0.5)

    def test_no_overlap(self) -> None:
        assert compute_overlap_ratio([(0, 1000)], 2000, 4000) == 0.0

    def test_multiple_intervals(self) -> None:
        assert compute_overlap_ratio([(0, 1000), (3000, 4000)], 0, 4000) == pytest.approx(0.5)


def _shots() -> list[dict]:
    return [
        {"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 2000},
        {"shotID": "SH0002", "finalStartMs": 2000, "finalEndMs": 4000},
    ]


class TestAggregateShotStates:
    def test_music_and_unknown_split(self) -> None:
        entries = aggregate_shot_states(
            _shots(),
            music_intervals=[(2000, 4000)],
            silent_intervals=[],
            texture_events=[],
            degraded=False,
        )
        assert entries[0]["state"] == "unknown"
        assert entries[0]["confidence"] == "unknown"
        assert entries[1]["state"] == "music"
        assert entries[1]["confidence"] == "high"
        assert entries[1]["musicOverlapRatio"] == pytest.approx(1.0)
        assert "100%" in entries[1]["basis"]

    def test_silent_ratio(self) -> None:
        entries = aggregate_shot_states(
            _shots(), music_intervals=[], silent_intervals=[(0, 2000)],
            texture_events=[], degraded=False,
        )
        assert entries[0]["state"] == "silent"
        assert entries[0]["silentOverlapRatio"] == pytest.approx(1.0)
        assert "静音" in entries[0]["basis"]

    def test_below_half_ratio_unknown(self) -> None:
        entries = aggregate_shot_states(
            _shots(), music_intervals=[(0, 900)], silent_intervals=[],
            texture_events=[], degraded=False,
        )
        assert entries[0]["state"] == "unknown"

    def test_degraded_downgrades_confidence_and_notes_basis(self) -> None:
        entries = aggregate_shot_states(
            _shots(), music_intervals=[(2000, 4000)], silent_intervals=[],
            texture_events=[], degraded=True,
        )
        assert entries[1]["state"] == "music"
        assert entries[1]["confidence"] == "medium"  # high 降一档
        assert "ASR 不可用" in entries[1]["basis"]

    def test_events_attached_to_shot(self) -> None:
        events = [
            {"atSec": 1.0, "kind": "textureRise", "flatnessDelta": 0.2, "label": "x"},
            {"atSec": 3.0, "kind": "textureFall", "flatnessDelta": -0.2, "label": "y"},
        ]
        entries = aggregate_shot_states(
            _shots(), music_intervals=[], silent_intervals=[],
            texture_events=events, degraded=False,
        )
        assert [e["kind"] for e in entries[0]["events"]] == ["textureRise"]
        assert [e["kind"] for e in entries[1]["events"]] == ["textureFall"]


class TestDetectMusicUnavailable:
    def test_no_audio_track(self, tmp_path) -> None:
        media = {
            "source": {
                "audioTracks": [],
                "analyzedRange": {"startMs": 0, "endMs": 4000},
            }
        }
        result = detect_music(tmp_path / "x.mp4", _shots(), None, media, DEFAULT_CONFIG)
        validate_artifact(ArtifactName.MUSIC_FLAGS, result)
        assert result["status"] == "unavailable"
        assert result["stateTally"] == {"music": 0, "silent": 0, "unknown": 2}
        for entry in result["shots"]:
            assert entry["state"] == "unknown"
            assert entry["confidence"] == "unknown"
            assert entry["musicOverlapRatio"] == 0.0
            assert entry["events"] == []
        assert result["speechGaps"] == []
        assert result["musicIntervals"] == []
