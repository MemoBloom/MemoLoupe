"""BGM 存在性检测（docs/03 §2.8，schemas/music-flags.json）。

算法概要（阈值均为 CALIBRATION，随产物保存在 thresholds）：

- ffmpeg 解码 mono s16le（默认 22050Hz），numpy STFT：Hann 窗 2048、
  hop 512（约 23ms），逐窗计算 levelDb（时域 RMS dB，满量程归一）、
  bassEnergy（0–250Hz 频带幅值和，采样按 1/32768 归一，单位随实现）、
  flatness（谱平坦度 = 几何均值 / 算术均值）。
- speechGaps：ASR complete 时取 transcript.segments 之间（含首尾外侧）
  ≥300ms 的间隙为锚点，测 median 电平/低频/平坦度；
  level ≥ musicLevelDb 或 bass ≥ musicBassEnergy → music；
  level ≤ silentLevelDb → silent；其余 unknown。
- ASR 非 complete → 降级：speechGaps=[]，逐窗分类后按全片纹理扫描
  生成区间（origin=fullRangeTexture），每镜头 confidence 降一档，
  basis 注明"ASR 不可用，降级为全片纹理分析"。
- textureEvents：全片 flatness 相邻窗突变（|Δ| ≥ 0.1，250ms 最小间隔）。
- 每镜头按与 music/silent 区间的时长重叠比例（≥0.5）裁定 state；
  stateTally 与 shots 聚合一致。state=unknown 不得转成 absent。

本文件只回答"有没有 BGM"；风格命名归 UnifiedMLLM 的 bgmStyle。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
from memoloupe.core.time_ranges import intersection_ms, seconds_to_ms
from memoloupe.media.audio_cuts import decode_mono_pcm
from memoloupe.media.concurrency import FFmpegPool

# 算法版本常量：pipeline 指纹引用，任何公式/阈值改动必须递增。
AUDIO_MUSIC_VERSION = "music.v1"

METHOD = "numpy STFT：语音间隙电平/低频判定 + 谱平坦度突变事件"

# CALIBRATION：STFT 参数与事件阈值。
WINDOW_SIZE = 2048
HOP_SIZE = 512
BASS_MAX_HZ = 250.0
MIN_GAP_MS = 300
FLATNESS_EVENT_DELTA = 0.1
FLATNESS_EVENT_MIN_SEPARATION_MS = 250

_SILENCE_FLOOR_DB = -99.0
_EPS = 1e-12


def stft_features(
    samples: np.ndarray, *, sample_rate: int, window: int = WINDOW_SIZE, hop: int = HOP_SIZE
) -> dict[str, np.ndarray]:
    """逐窗 STFT 特征：timesSec（窗中心秒）、levelDb、bassEnergy、flatness。"""
    empty = {
        "timesSec": np.zeros(0),
        "levelDb": np.zeros(0),
        "bassEnergy": np.zeros(0),
        "flatness": np.zeros(0),
    }
    if len(samples) < window:
        return empty
    n_frames = 1 + (len(samples) - window) // hop
    x = samples[: (n_frames - 1) * hop + window].astype(np.float64) / 32768.0
    # (n_frames, window) 滑动窗视图
    frames = np.lib.stride_tricks.as_strided(
        x,
        shape=(n_frames, window),
        strides=(hop * x.strides[0], x.strides[0]),
    )
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    floor = 10.0 ** (_SILENCE_FLOOR_DB / 20.0)
    level_db = 20.0 * np.log10(np.maximum(rms, floor))

    spectrum = np.abs(np.fft.rfft(frames * np.hanning(window), axis=1))
    freqs = np.fft.rfftfreq(window, d=1.0 / sample_rate)
    bass_energy = spectrum[:, freqs <= BASS_MAX_HZ].sum(axis=1)
    geo = np.exp(np.mean(np.log(spectrum + _EPS), axis=1))
    flatness = geo / (np.mean(spectrum, axis=1) + _EPS)

    times_sec = (np.arange(n_frames) * hop + window / 2) / sample_rate
    return {
        "timesSec": times_sec,
        "levelDb": level_db,
        "bassEnergy": bass_energy,
        "flatness": flatness,
    }


def find_speech_gaps(
    segments: list[dict],
    *,
    range_start_ms: int,
    range_end_ms: int,
    min_gap_ms: int = MIN_GAP_MS,
) -> list[tuple[int, int]]:
    """ASR segments 的补集间隙（含首尾外侧），只保留 ≥ min_gap_ms 的。"""
    gaps: list[tuple[int, int]] = []
    cursor = range_start_ms
    for segment in sorted(segments, key=lambda s: int(s["startMs"])):
        seg_start = max(range_start_ms, int(segment["startMs"]))
        seg_end = min(range_end_ms, int(segment["endMs"]))
        if seg_start - cursor >= min_gap_ms:
            gaps.append((cursor, seg_start))
        cursor = max(cursor, seg_end)
    if range_end_ms - cursor >= min_gap_ms:
        gaps.append((cursor, range_end_ms))
    return gaps


def classify_gap(level_db: float, bass_energy: float, thresholds: dict) -> str:
    """gap 三态判定：电平或低频达标 → music；低于静音线 → silent。"""
    if level_db >= thresholds["musicLevelDb"] or bass_energy >= thresholds["musicBassEnergy"]:
        return "music"
    if level_db <= thresholds["silentLevelDb"]:
        return "silent"
    return "unknown"


def detect_texture_events(
    times_sec: list[float] | np.ndarray,
    flatness: list[float] | np.ndarray,
    *,
    delta_threshold: float = FLATNESS_EVENT_DELTA,
    min_separation_ms: int = FLATNESS_EVENT_MIN_SEPARATION_MS,
) -> list[dict]:
    """谱平坦度突变事件：|Δ| ≥ 阈值处取 |Δ| 最大者，按最小间隔抑制。"""
    values = np.asarray(flatness, dtype=np.float64)
    times = np.asarray(times_sec, dtype=np.float64)
    if len(values) < 2:
        return []
    deltas = np.diff(values)
    candidates = [
        (i + 1, float(deltas[i]))
        for i in range(len(deltas))
        if abs(float(deltas[i])) >= delta_threshold
    ]
    min_sep_sec = min_separation_ms / 1000.0
    accepted: list[tuple[int, float]] = []
    for index, delta in sorted(candidates, key=lambda c: -abs(c[1])):
        if all(abs(times[index] - times[kept]) >= min_sep_sec for kept, _ in accepted):
            accepted.append((index, delta))
    accepted.sort(key=lambda c: times[c[0]])
    events: list[dict] = []
    for index, delta in accepted:
        rise = delta > 0
        events.append(
            {
                "atSec": round(float(times[index]), 3),
                "kind": "textureRise" if rise else "textureFall",
                "flatnessDelta": round(delta, 3),
                "label": "音质地变粗糙（疑似音乐/打击乐进入）"
                if rise
                else "音质地变平滑（疑似音乐/打击乐退出）",
            }
        )
    return events


def compute_overlap_ratio(
    intervals: list[tuple[int, int]], start_ms: int, end_ms: int
) -> float:
    """半开区间集合与 [start_ms, end_ms) 的交集时长占比（0..1）。"""
    duration = end_ms - start_ms
    if duration <= 0:
        return 0.0
    overlap = sum(
        intersection_ms(start_ms, end_ms, iv_start, iv_end)
        for iv_start, iv_end in intervals
    )
    return min(1.0, max(0.0, overlap / duration))


def aggregate_shot_states(
    shots: list[dict],
    *,
    music_intervals: list[tuple[int, int]],
    silent_intervals: list[tuple[int, int]],
    texture_events: list[dict],
    degraded: bool,
) -> list[dict]:
    """按区间重叠比例聚合每镜头 BGM 状态（docs/03 §2.8）。

    ratio ≥ 0.5 → 对应 state，confidence high；不足 → unknown/unknown。
    degraded（ASR 不可用降级全片纹理分析）时 confidence 降一档，
    basis 注明降级原因。
    """
    entries: list[dict] = []
    for shot in shots:
        start_ms = int(shot["finalStartMs"])
        end_ms = int(shot["finalEndMs"])
        music_ratio = compute_overlap_ratio(music_intervals, start_ms, end_ms)
        silent_ratio = compute_overlap_ratio(silent_intervals, start_ms, end_ms)
        if music_ratio >= 0.5:
            state = "music"
            confidence = "high"
            basis = f"镜头有 {round(music_ratio * 100)}% 时长落在实测音乐区间内。"
        elif silent_ratio >= 0.5:
            state = "silent"
            confidence = "high"
            basis = f"镜头有 {round(silent_ratio * 100)}% 时长落在实测静音区间内。"
        else:
            state = "unknown"
            confidence = "unknown"
            basis = "音频信号不足，无法确认是否有背景音乐。"
        if degraded:
            if confidence == "high":
                confidence = "medium"
            basis += "ASR 不可用，降级为全片纹理分析。"
        events = [
            event
            for event in texture_events
            if start_ms <= seconds_to_ms(event["atSec"]) < end_ms
        ]
        entries.append(
            {
                "shotID": shot["shotID"],
                "startMs": start_ms,
                "endMs": end_ms,
                "state": state,
                "confidence": confidence,
                "basis": basis,
                "musicOverlapRatio": round(music_ratio, 3),
                "silentOverlapRatio": round(silent_ratio, 3),
                "events": events,
            }
        )
    return entries


def _gap_entry(
    start_ms: int, end_ms: int, feats: dict[str, np.ndarray], sample_rate: int, thresholds: dict
) -> dict:
    """单个语音间隙的测量与三态判定；无窗覆盖时 state=unknown。"""
    center_ms = feats["timesSec"] * 1000.0
    mask = (center_ms >= start_ms) & (center_ms < end_ms)
    entry: dict = {
        "startSec": round(start_ms / 1000.0, 3),
        "endSec": round(end_ms / 1000.0, 3),
        "state": "unknown",
    }
    if not np.any(mask):
        return entry
    level = float(np.median(feats["levelDb"][mask]))
    bass = float(np.median(feats["bassEnergy"][mask]))
    flatness = float(np.median(feats["flatness"][mask]))
    entry["state"] = classify_gap(level, bass, thresholds)
    entry["measurements"] = {
        "medianLevelDb": round(level, 3),
        "medianBassEnergy": round(bass, 3),
        "medianFlatness": round(flatness, 3),
    }
    return entry


def _texture_scan_intervals(
    feats: dict[str, np.ndarray], sample_rate: int, thresholds: dict
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """降级路径：逐窗三态分类，连续同态窗合并为全片纹理区间。"""
    n = len(feats["levelDb"])
    states = [
        classify_gap(float(feats["levelDb"][i]), float(feats["bassEnergy"][i]), thresholds)
        for i in range(n)
    ]
    window_sec = WINDOW_SIZE / sample_rate
    music: list[tuple[int, int]] = []
    silent: list[tuple[int, int]] = []
    i = 0
    while i < n:
        if states[i] not in ("music", "silent"):
            i += 1
            continue
        j = i
        while j + 1 < n and states[j + 1] == states[i]:
            j += 1
        start_sec = max(0.0, float(feats["timesSec"][i]) - window_sec / 2)
        end_sec = float(feats["timesSec"][j]) + window_sec / 2
        target = music if states[i] == "music" else silent
        target.append((seconds_to_ms(start_sec), seconds_to_ms(end_sec)))
        i = j + 1
    return music, silent


def _unavailable_result(shots: list[dict], thresholds: dict) -> dict:
    entries = [
        {
            "shotID": shot["shotID"],
            "startMs": int(shot["finalStartMs"]),
            "endMs": int(shot["finalEndMs"]),
            "state": "unknown",
            "confidence": "unknown",
            "basis": "无音轨，BGM 检测不可用。",
            "musicOverlapRatio": 0.0,
            "silentOverlapRatio": 0.0,
            "events": [],
        }
        for shot in shots
    ]
    return {
        "status": "unavailable",
        "method": METHOD,
        "stateTally": {"music": 0, "silent": 0, "unknown": len(entries)},
        "thresholds": thresholds,
        "speechGaps": [],
        "textureEvents": [],
        "musicIntervals": [],
        "shots": entries,
    }


def detect_music(
    source: Path,
    shots: list[dict],
    asr: dict | None,
    media: dict,
    config: dict,
    *,
    pool: FFmpegPool | None = None,
) -> dict:
    """检测每镜头 BGM 状态，返回符合 music-flags.json 的 dict。"""
    source = Path(source)
    music_cfg = config.get("music", {})
    sample_rate = int(music_cfg.get("sampleRate", 22050))
    thresholds = {
        "sampleRate": sample_rate,
        "musicLevelDb": float(music_cfg.get("musicLevelDb", -18.0)),
        "musicBassEnergy": float(music_cfg.get("musicBassEnergy", 150.0)),
        "silentLevelDb": float(music_cfg.get("silentLevelDb", -22.0)),
    }

    if not media.get("source", {}).get("audioTracks"):
        result = _unavailable_result(shots, thresholds)
        validate_artifact(ArtifactName.MUSIC_FLAGS, result)
        return result

    analyzed = media.get("source", {}).get("analyzedRange", {"startMs": 0, "endMs": 0})
    start_ms = int(analyzed["startMs"])
    end_ms = int(analyzed["endMs"])

    samples = decode_mono_pcm(
        source,
        sample_rate=sample_rate,
        start_ms=start_ms,
        end_ms=end_ms,
        config=config,
        pool=pool,
    )
    feats = stft_features(samples, sample_rate=sample_rate)
    texture_events = detect_texture_events(feats["timesSec"], feats["flatness"])

    asr_complete = isinstance(asr, dict) and asr.get("status") == "complete"
    speech_gaps: list[dict] = []
    music_intervals: list[tuple[int, int]]
    silent_intervals: list[tuple[int, int]]
    interval_origin: str
    if asr_complete:
        segments = asr.get("transcript", {}).get("segments", [])
        gaps = find_speech_gaps(
            segments, range_start_ms=start_ms, range_end_ms=end_ms
        )
        music_intervals = []
        silent_intervals = []
        for gap_start, gap_end in gaps:
            gap = _gap_entry(gap_start, gap_end, feats, sample_rate, thresholds)
            speech_gaps.append(gap)
            if gap["state"] == "music":
                music_intervals.append((gap_start, gap_end))
            elif gap["state"] == "silent":
                silent_intervals.append((gap_start, gap_end))
        interval_origin = "gapAnchor"
    else:
        music_intervals, silent_intervals = _texture_scan_intervals(
            feats, sample_rate, thresholds
        )
        interval_origin = "fullRangeTexture"

    entries = aggregate_shot_states(
        shots,
        music_intervals=music_intervals,
        silent_intervals=silent_intervals,
        texture_events=texture_events,
        degraded=not asr_complete,
    )
    tally = {"music": 0, "silent": 0, "unknown": 0}
    for entry in entries:
        tally[entry["state"]] += 1

    result = {
        "status": "complete",
        "method": METHOD,
        "stateTally": tally,
        "thresholds": thresholds,
        "speechGaps": speech_gaps,
        "textureEvents": texture_events,
        "musicIntervals": [
            {
                "startSec": round(iv_start / 1000.0, 3),
                "endSec": round(iv_end / 1000.0, 3),
                "origin": interval_origin,
            }
            for iv_start, iv_end in music_intervals
        ],
        "shots": entries,
    }
    validate_artifact(ArtifactName.MUSIC_FLAGS, result)
    return result
