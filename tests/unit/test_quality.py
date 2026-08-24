"""media/quality.py 单元测试：filter 输出解析、事件映射、flag 与置信度规则。"""

from __future__ import annotations

from memoloupe.media.quality import (
    QUALITY_DETECTION_VERSION,
    build_shot_entry,
    parse_audio_peaks,
    parse_blackdetect_events,
    parse_freezedetect_events,
    parse_video_samples,
)

THRESHOLDS = {
    "videoSampleFps": 2.0,
    "blurFlagThreshold": 11.0,
    "underexposedYAVG": 40.0,
    "overexposedYAVG": 215.0,
    "audioClipPeakDb": -0.1,
}

# 真实 ffmpeg 8.1 metadata=mode=print:file=- 输出样式（节选）
VIDEO_PRINT = """\
frame:0    pts:0       pts_time:0
lavfi.blur=nan
lavfi.signalstats.YAVG=81
lavfi.signalstats.SATAVG=118
frame:1    pts:1       pts_time:0.5
lavfi.blur=12.75
lavfi.signalstats.YAVG=30.5
frame:2    pts:2       pts_time:1
lavfi.blur=5.25
lavfi.signalstats.YAVG=220.0
"""

BLACK_STDERR = """\
[Parsed_blackdetect_3 @ 0x7b48c3e040] black_start:1 black_end:2 black_duration:1
"""

FREEZE_STDERR = """\
[Parsed_freezedetect_4 @ 0x7a02c4a040] lavfi.freezedetect.freeze_start: 0.5
[Parsed_freezedetect_4 @ 0x7a02c4a040] lavfi.freezedetect.freeze_end: 2.5
[Parsed_freezedetect_4 @ 0x7a02c4a040] lavfi.freezedetect.freeze_duration: 2
"""

FREEZE_UNCLOSED_STDERR = """\
[Parsed_freezedetect_0 @ 0x7646c69d40] lavfi.freezedetect.freeze_start: 0
"""

AUDIO_PRINT = """\
frame:0    pts:0       pts_time:0
lavfi.astats.Overall.Peak_level=-18.063656
frame:1    pts:1024    pts_time:0.064
lavfi.astats.Overall.Peak_level=-0.05
"""

SHOTS = [
    {"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 1000},
    {"shotID": "SH0002", "finalStartMs": 1000, "finalEndMs": 2000},
]


def test_version_constant() -> None:
    assert QUALITY_DETECTION_VERSION == "quality.v1"


def test_parse_video_samples_machine_keys() -> None:
    samples = parse_video_samples(VIDEO_PRINT)
    assert [s["timeMs"] for s in samples] == [0, 500, 1000]
    # nan blur 按缺失处理
    assert samples[0]["blur"] is None
    assert samples[0]["yavg"] == 81.0
    assert samples[1]["blur"] == 12.75
    assert samples[2]["yavg"] == 220.0


def test_parse_blackdetect_events() -> None:
    assert parse_blackdetect_events(BLACK_STDERR) == [(1000, 2000)]
    assert parse_blackdetect_events("no events here") == []


def test_parse_freezedetect_events() -> None:
    assert parse_freezedetect_events(FREEZE_STDERR) == [(500, 2500)]
    # EOF 时 freeze 未闭合：endMs 为 None，按延伸到片尾处理
    assert parse_freezedetect_events(FREEZE_UNCLOSED_STDERR) == [(0, None)]


def test_parse_audio_peaks_db() -> None:
    peaks = parse_audio_peaks(AUDIO_PRINT)
    assert peaks == [(0, -18.063656), (64, -0.05)]


def _entry(shot, samples, peaks=(), black=(), freeze=(), has_audio=True):
    return build_shot_entry(
        shot,
        video_samples=samples,
        audio_peaks=peaks,
        black_events=black,
        freeze_events=freeze,
        has_audio=has_audio,
        thresholds=THRESHOLDS,
    )


def test_flags_blur_underexposed_overexposed() -> None:
    shot = {"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 1000}
    samples = [
        {"timeMs": 0, "blur": 12.0, "yavg": 30.0},
        {"timeMs": 500, "blur": 12.5, "yavg": 31.0},
    ]
    entry = _entry(shot, samples)
    assert "画面模糊" in entry["flags"]  # medianBlur >= 11
    assert "欠曝" in entry["flags"]  # medianYAVG <= 40
    assert "过曝" not in entry["flags"]
    assert entry["confidence"] == "high"
    assert entry["measurements"]["videoSampleCount"] == 2
    assert entry["measurements"]["medianBlur"] == 12.25

    dark = _entry(shot, [
        {"timeMs": 0, "blur": 5.0, "yavg": 215.0},
        {"timeMs": 500, "blur": 5.0, "yavg": 216.0},
    ])
    assert "过曝" in dark["flags"]  # medianYAVG >= 215
    assert "画面模糊" not in dark["flags"]


def test_audio_clipping_flag_and_absent_audio() -> None:
    shot = {"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 1000}
    samples = [
        {"timeMs": 0, "blur": 5.0, "yavg": 100.0},
        {"timeMs": 500, "blur": 5.0, "yavg": 100.0},
    ]
    clipped = _entry(shot, samples, peaks=[(64, -0.05)])
    assert "音频削波" in clipped["flags"]  # peak >= -0.1 dB（CALIBRATION）

    ok = _entry(shot, samples, peaks=[(64, -3.0)])
    assert "音频削波" not in ok["flags"]

    # 无音轨：即使调用方误传峰值也绝不报削波
    no_audio = _entry(shot, samples, peaks=[(64, 0.0)], has_audio=False)
    assert "音频削波" not in no_audio["flags"]
    assert "audioSampleCount" not in no_audio["measurements"]


def test_black_and_freeze_events_overlap() -> None:
    shot = {"shotID": "SH0002", "finalStartMs": 1000, "finalEndMs": 2000}
    samples = [
        {"timeMs": 1000, "blur": 5.0, "yavg": 100.0},
        {"timeMs": 1500, "blur": 5.0, "yavg": 100.0},
    ]
    entry = _entry(shot, samples, black=[(1000, 2000)], freeze=[(1200, 1800)])
    assert "黑场" in entry["flags"]
    assert "画面冻结" in entry["flags"]

    other = {"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 1000}
    entry2 = _entry(other, samples, black=[(1000, 2000)])
    assert "黑场" not in entry2["flags"]

    # 未闭合 freeze 延伸到片尾
    entry3 = _entry(other, samples, freeze=[(500, None)])
    assert "画面冻结" in entry3["flags"]


def test_insufficient_samples_confidence_unknown() -> None:
    shot = {"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 1000}
    one = _entry(shot, [{"timeMs": 0, "blur": 5.0, "yavg": 100.0}])
    assert one["confidence"] == "unknown"
    zero = _entry(shot, [])
    assert zero["confidence"] == "unknown"
    assert zero["flags"] == []
    assert zero["measurements"]["videoSampleCount"] == 0
    # 没有样本时不得伪造 median 数值
    assert "medianBlur" not in zero["measurements"]
    assert "medianYAVG" not in zero["measurements"]


def test_flag_order_is_stable() -> None:
    shot = {"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 1000}
    samples = [
        {"timeMs": 0, "blur": 12.0, "yavg": 30.0},
        {"timeMs": 500, "blur": 12.0, "yavg": 30.0},
    ]
    entry = _entry(shot, samples, peaks=[(0, 0.0)], black=[(0, 1000)], freeze=[(0, None)])
    assert entry["flags"] == ["画面模糊", "欠曝", "音频削波", "黑场", "画面冻结"]
