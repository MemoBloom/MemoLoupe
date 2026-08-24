"""media/audio_energy.py 单元测试：RMS dB 计算、label 边界、窗聚合。"""

from __future__ import annotations

import math
from array import array

import pytest

from memoloupe.media.audio_energy import (
    AUDIO_ENERGY_VERSION,
    SILENCE_FLOOR_DB,
    aggregate_shot_windows,
    assign_windows_to_shots,
    label_for_db,
    rms_db,
    window_db_values,
)

THRESHOLDS = {"silent": -60.0, "low": -40.0, "medium": -25.0, "high": -12.0}


def _sine_window(amplitude: int, n: int = 320) -> array:
    # 16kHz 下 320 采样 = 20ms；取 50Hz 恰好一个整周期，避免截断误差
    return array(
        "h", [int(round(amplitude * math.sin(2 * math.pi * i / n))) for i in range(n)]
    )


def test_version_constant() -> None:
    assert AUDIO_ENERGY_VERSION == "energy.v1"


def test_rms_db_known_sine() -> None:
    # 半幅正弦：rms = 16384/√2，dB = 20*log10(rms/32768) ≈ -9.03
    db = rms_db(_sine_window(16384))
    assert db == pytest.approx(-9.03, abs=0.01)


def test_rms_db_full_scale_sine_near_zero() -> None:
    db = rms_db(_sine_window(32767))
    assert db == pytest.approx(-3.01, abs=0.01)


def test_rms_db_silence_floor() -> None:
    # 全零窗按静音地板值处理，不产生 -inf
    assert rms_db(array("h", [0] * 320)) == SILENCE_FLOOR_DB == -99.0


def test_label_boundaries_strict_less_than() -> None:
    # 边界归属：小于号严格，等于归较高档
    assert label_for_db(-99.0, THRESHOLDS) == "静音"
    assert label_for_db(-60.5, THRESHOLDS) == "静音"
    assert label_for_db(-60.0, THRESHOLDS) == "低"
    assert label_for_db(-40.5, THRESHOLDS) == "低"
    assert label_for_db(-40.0, THRESHOLDS) == "中"
    assert label_for_db(-25.5, THRESHOLDS) == "中"
    assert label_for_db(-25.0, THRESHOLDS) == "高"
    assert label_for_db(-12.5, THRESHOLDS) == "高"
    assert label_for_db(-12.0, THRESHOLDS) == "峰值"
    assert label_for_db(-3.0, THRESHOLDS) == "峰值"


def test_window_db_values_splits_20ms() -> None:
    # 640 采样 = 两个 20ms 窗：一个静音、一个半幅正弦
    pcm = array("h", [0] * 320) + _sine_window(16384)
    values = window_db_values(pcm.tobytes(), sample_rate=16000, frame_ms=20)
    assert len(values) == 2
    assert values[0] == SILENCE_FLOOR_DB
    assert values[1] == pytest.approx(-9.03, abs=0.01)


def test_window_db_values_drops_partial_tail() -> None:
    # 不足一窗的尾巴丢弃，不伪造样本
    pcm = array("h", [1000] * (320 + 100))
    values = window_db_values(pcm.tobytes(), sample_rate=16000, frame_ms=20)
    assert len(values) == 1


def test_assign_windows_by_start_time() -> None:
    shots = [
        {"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 1000},
        {"shotID": "SH0002", "finalStartMs": 1000, "finalEndMs": 2000},
    ]
    values = [-30.0] * 100  # 100 个 20ms 窗
    assigned = assign_windows_to_shots(values, frame_ms=20, shots=shots)
    assert len(assigned["SH0001"]) == 50
    assert len(assigned["SH0002"]) == 50


def test_aggregate_shot_stats() -> None:
    entry = aggregate_shot_windows("SH0001", [-30.0, -10.0, -20.0], THRESHOLDS)
    assert entry["shotID"] == "SH0001"
    assert entry["frameCount"] == 3
    assert entry["medianDb"] == -20.0
    assert entry["minDb"] == -30.0
    assert entry["maxDb"] == -10.0
    assert entry["label"] == "高"  # -20 < -12 且 >= -25


def test_aggregate_empty_shot_no_fake_numbers() -> None:
    entry = aggregate_shot_windows("SH0001", [], THRESHOLDS)
    assert entry["medianDb"] is None
    assert entry["frameCount"] == 0
    assert entry["label"] == "unknown"
    assert "minDb" not in entry
    assert "maxDb" not in entry
