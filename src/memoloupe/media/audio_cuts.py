"""音频切点检测与音画边界关联（docs/03 §2.4，schemas/audio-cuts.json）。

算法概要：

- ffmpeg 解码首选音轨为 mono s16le PCM（分析采样率默认 16000Hz），
  numpy 按固定 20ms 帧计算六特征：rmsDb、zeroCrossingRate、roughness、
  amplitudeShape、autocorrelation1ms、autocorrelation4ms。
- 相邻帧特征差按 ``max(MINIMUM_SCALES, 局部尺度)`` 归一后加权求和得到
  novelty score；局部尺度取该 pair 前后 ±1s 窗内 |Δ| 的中位数（CALIBRATION）。
- 峰值选择：score > threshold 且为局部最大；随后按
  ``associationWindowMs / 2`` 的最小间隔抑制，分高者优先（CALIBRATION）。
- 每个内部视觉边界在关联窗内取 score 最高的音频切点：
  |offset| ≤ syncToleranceMs → synchronizedCut；窗内无切点 →
  pictureCutAudioContinuous；有切点但超出同步窗 → audioBoundaryUndetermined。
- ``align_boundaries=True`` 时，仅 synchronizedCut 且音频切点
  confidence=high 才规划移动，且必须满足最短镜头约束；本模块只返回
  ``movedBoundaries`` 列表，不改写 shots.json，detected 边界永不修改。

特征精确定义均为 CALIBRATION（docs/03 §2.4）：roughness 用帧内 4 子窗
RMS 包络的变异系数近似幅值调制深度；amplitudeShape 用峰均比（crest
factor）；自相关为帧内 1ms/4ms 滞后归一化自相关。特征提取器
（:func:`compute_frame_features`）与峰值选择器（:func:`select_peaks`）
拆分为独立函数，便于替换。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
from memoloupe.core.ids import make_audio_boundary_id
from memoloupe.media.concurrency import FFmpegPool
from memoloupe.media.proc import run_process

# 算法版本常量：pipeline 指纹引用，任何公式/阈值改动必须递增。
AUDIO_CUTS_VERSION = "audio-cuts.v1"

FEATURE_NAMES = (
    "rmsDb",
    "zeroCrossingRate",
    "roughness",
    "amplitudeShape",
    "autocorrelation1ms",
    "autocorrelation4ms",
)
MINIMUM_SCALES = (1.5, 0.015, 0.04, 0.025, 0.04, 0.04)
FEATURE_WEIGHTS = (1.0, 0.8, 0.8, 0.6, 0.8, 0.8)

METHOD = "audioFeatureNoveltyHardCutCandidates"

# CALIBRATION：音频切点 confidence 分档阈值。
_HIGH_CONFIDENCE_SCORE = 12.0
# CALIBRATION：novelty 局部尺度的窗半径（毫秒，换算为 pair 数）。
_LOCAL_SCALE_WINDOW_MS = 1000
# 静音地板（与 audio_energy 一致，避免 log10(0)）。
_SILENCE_FLOOR_DB = -99.0
_EPS = 1e-12


def decode_mono_pcm(
    source: Path,
    *,
    sample_rate: int,
    start_ms: int,
    end_ms: int,
    config: dict,
    pool: FFmpegPool | None,
) -> np.ndarray:
    """解码 [start_ms, end_ms) 范围为 mono s16le 的 int16 numpy 数组。"""
    ffmpeg_cfg = config.get("ffmpeg", {})
    argv = [
        str(ffmpeg_cfg.get("ffmpegPath", "ffmpeg")),
        "-hide_banner", "-loglevel", "error",
        "-ss", f"{start_ms / 1000.0:.3f}",
        "-i", str(source),
        "-t", f"{(end_ms - start_ms) / 1000.0:.3f}",
        "-vn", "-f", "s16le", "-ac", "1", "-ar", str(sample_rate),
        "-",
    ]
    runner = pool.run if pool is not None else run_process
    result = runner(argv, timeout_sec=float(ffmpeg_cfg.get("scanTimeoutSec", 300.0)))
    return np.frombuffer(result.stdout, dtype="<i2").copy()


def compute_frame_features(
    samples: np.ndarray, *, sample_rate: int, frame_ms: int
) -> np.ndarray:
    """按 frame_ms 帧计算六特征，返回 shape (n_frames, 6) 的 float64 数组。

    不足一帧的尾巴丢弃；全零帧不产生 NaN（rmsDb 取静音地板，自相关取 0）。
    """
    frame_len = sample_rate * frame_ms // 1000
    if frame_len < 2:
        raise ValueError(f"帧长过小: sample_rate={sample_rate}, frame_ms={frame_ms}")
    n_frames = len(samples) // frame_len
    if n_frames == 0:
        return np.zeros((0, len(FEATURE_NAMES)), dtype=np.float64)
    x = samples[: n_frames * frame_len].reshape(n_frames, frame_len).astype(np.float64)
    x /= 32768.0

    # rmsDb：20*log10(rms/满量程)，地板 -99 dB。
    rms = np.sqrt(np.mean(x * x, axis=1))
    floor = 10.0 ** (_SILENCE_FLOOR_DB / 20.0)
    rms_db = 20.0 * np.log10(np.maximum(rms, floor))

    # zeroCrossingRate：符号变化比例。
    signs = np.signbit(x)
    zcr = np.mean(signs[:, 1:] != signs[:, :-1], axis=1)

    # roughness（CALIBRATION）：帧内 4 子窗 RMS 包络的变异系数。
    sub = x.reshape(n_frames, 4, frame_len // 4)
    env = np.sqrt(np.mean(sub * sub, axis=2))
    roughness = np.std(env, axis=1) / np.maximum(np.mean(env, axis=1), _EPS)

    # amplitudeShape（CALIBRATION）：峰均比 peak/rms。
    peak = np.max(np.abs(x), axis=1)
    amplitude_shape = peak / np.maximum(rms, _EPS)

    # autocorrelation1ms/4ms：帧内滞后归一化自相关。
    lag1 = max(1, round(sample_rate * 0.001))
    lag4 = max(1, round(sample_rate * 0.004))
    ac1 = _normalized_autocorrelation(x, lag1)
    ac4 = _normalized_autocorrelation(x, lag4)

    return np.column_stack([rms_db, zcr, roughness, amplitude_shape, ac1, ac4])


def _normalized_autocorrelation(x: np.ndarray, lag: int) -> np.ndarray:
    """帧内滞后 lag 的归一化自相关；零能量帧返回 0。"""
    a = x[:, :-lag]
    b = x[:, lag:]
    num = np.mean(a * b, axis=1)
    den = np.sqrt(np.mean(a * a, axis=1) * np.mean(b * b, axis=1))
    return np.where(den > _EPS, num / np.maximum(den, _EPS), 0.0)


def novelty_scores(
    features: np.ndarray,
    *,
    minimum_scales: tuple[float, ...] = MINIMUM_SCALES,
    weights: tuple[float, ...] = FEATURE_WEIGHTS,
    local_scale_window_pairs: int | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """相邻帧归一化特征差的加权 novelty score。

    返回 ``(scores, raw_deltas)``：``scores[p]`` 对应 frame[p] → frame[p+1]
    的变化（边界帧为 p+1）；``raw_deltas[p]`` 为该 pair 的六特征绝对差。
    归一化分母为 ``max(minimum_scale, 局部尺度)``；局部尺度取 pair 前后
    ±local_scale_window_pairs 内 |Δ| 的中位数（CALIBRATION）。
    """
    n_frames = len(features)
    if n_frames < 2:
        return np.zeros((0,), dtype=np.float64), np.zeros(
            (0, len(minimum_scales)), dtype=np.float64
        )
    if local_scale_window_pairs is None:
        local_scale_window_pairs = 50  # 20ms 帧下 ≈ ±1s
    deltas = np.abs(np.diff(features, axis=0))  # (n-1, n_features)
    mins = np.asarray(minimum_scales, dtype=np.float64)
    w = np.asarray(weights, dtype=np.float64)

    n_pairs = len(deltas)
    scales = np.empty_like(deltas)
    for p in range(n_pairs):
        lo = max(0, p - local_scale_window_pairs)
        hi = min(n_pairs, p + local_scale_window_pairs + 1)
        scales[p] = np.median(deltas[lo:hi], axis=0)
    scales = np.maximum(scales, mins)
    scores = (deltas / scales) @ w
    return scores, deltas


def select_peaks(
    scores: np.ndarray | list[float],
    *,
    frame_ms: int,
    threshold: float,
    min_separation_ms: int,
) -> list[dict]:
    """novelty 峰值选择：score > threshold 且局部最大，再做最小间隔抑制。

    返回按 pairIndex 升序的 ``{"pairIndex", "score"}`` 列表。
    """
    values = np.asarray(scores, dtype=np.float64)
    n = len(values)
    if n == 0:
        return []
    candidates: list[tuple[int, float]] = []
    for p in range(n):
        score = float(values[p])
        if score <= threshold:
            continue
        left = float(values[p - 1]) if p > 0 else -np.inf
        right = float(values[p + 1]) if p < n - 1 else -np.inf
        if score >= left and score >= right and (score > left or score > right):
            candidates.append((p, score))

    min_sep_pairs = max(1, round(min_separation_ms / frame_ms))
    accepted: list[tuple[int, float]] = []
    for pair_index, score in sorted(candidates, key=lambda c: -c[1]):
        if all(abs(pair_index - kept[0]) >= min_sep_pairs for kept in accepted):
            accepted.append((pair_index, score))
    accepted.sort()
    return [{"pairIndex": p, "score": s} for p, s in accepted]


def classify_visual_boundary(
    visual_time_ms: int,
    audio_boundaries: list[dict],
    *,
    sync_tolerance_ms: int,
    association_window_ms: int,
) -> dict:
    """单个内部视觉边界的音画关联分类（docs/03 §2.4 步骤 6-9）。"""
    in_window = [
        b
        for b in audio_boundaries
        if abs(int(b["timeMs"]) - visual_time_ms) <= association_window_ms
    ]
    if not in_window:
        return {
            "classification": "pictureCutAudioContinuous",
            "labelZh": "画面切点，混音层未测到音频切点",
            "visualTimeMs": visual_time_ms,
            "confidence": "medium",
        }
    best = max(in_window, key=lambda b: float(b["score"]))
    offset = int(best["timeMs"]) - visual_time_ms
    if abs(offset) <= sync_tolerance_ms:
        return {
            "classification": "synchronizedCut",
            "labelZh": f"音画同步切（偏差 {offset} ms）",
            "visualTimeMs": visual_time_ms,
            "audioTimeMs": int(best["timeMs"]),
            "offsetMs": offset,
            "confidence": best["confidence"],
            "audioBoundaryID": best["audioBoundaryID"],
            "audioBoundaryScore": float(best["score"]),
        }
    return {
        "classification": "audioBoundaryUndetermined",
        "labelZh": f"检测到音频切点但超出同步窗，无法归因（偏差 {offset} ms）",
        "visualTimeMs": visual_time_ms,
        "audioTimeMs": int(best["timeMs"]),
        "offsetMs": offset,
        "confidence": "low",
        "audioBoundaryID": best["audioBoundaryID"],
        "audioBoundaryScore": float(best["score"]),
    }


def plan_boundary_alignment(
    internal_boundaries: list[dict],
    shots: list[dict],
    *,
    min_shot_ms: int,
) -> list[dict]:
    """规划 final 边界移动（docs/03 §2.4 final 边界规则）。

    仅 synchronizedCut 且 confidence=high 的边界可移动；移动后两侧镜头
    时长都必须 >= min_shot_ms，否则不动。本函数只返回移动计划，
    不修改任何 shots 数据。
    """
    moved: list[dict] = []
    for i, boundary in enumerate(internal_boundaries):
        if boundary.get("classification") != "synchronizedCut":
            continue
        if boundary.get("confidence") != "high":
            continue
        new_time = boundary.get("audioTimeMs")
        if not isinstance(new_time, int):
            continue
        visual = int(boundary["visualTimeMs"])
        if new_time == visual:
            continue
        left = shots[i]
        right = shots[i + 1]
        if new_time - int(left["finalStartMs"]) < min_shot_ms:
            continue
        if int(right["finalEndMs"]) - new_time < min_shot_ms:
            continue
        moved.append(
            {
                "visualTimeMs": visual,
                "audioTimeMs": new_time,
                "offsetMs": int(boundary["offsetMs"]),
                "audioBoundaryID": boundary["audioBoundaryID"],
                "leftShotID": left["shotID"],
                "rightShotID": right["shotID"],
            }
        )
    return moved


def _source_start_side(visual_time_ms: int) -> dict:
    return {
        "classification": "sourceStart",
        "labelZh": "片头（音画同时开始）",
        "visualTimeMs": visual_time_ms,
        "confidence": "high",
    }


def _source_end_side(visual_time_ms: int) -> dict:
    return {
        "classification": "sourceEnd",
        "labelZh": "片尾（音画同时结束）",
        "visualTimeMs": visual_time_ms,
        "confidence": "high",
    }


def _unavailable_side(visual_time_ms: int) -> dict:
    return {
        "classification": "unavailable",
        "labelZh": "音轨不可用，无法检测音频切点",
        "visualTimeMs": visual_time_ms,
        "confidence": "low",
    }


def _assemble_shot_entries(shots: list[dict], internal_sides: list[dict] | None) -> list[dict]:
    """组装 per-shot boundaryIn/boundaryOut；internal_sides=None 表示无音轨。"""
    last = len(shots) - 1
    entries: list[dict] = []
    for i, shot in enumerate(shots):
        if i == 0:
            boundary_in = _source_start_side(int(shot["finalStartMs"]))
        elif internal_sides is None:
            boundary_in = _unavailable_side(int(shot["finalStartMs"]))
        else:
            boundary_in = internal_sides[i - 1]
        if i == last:
            boundary_out = _source_end_side(int(shot["finalEndMs"]))
        elif internal_sides is None:
            boundary_out = _unavailable_side(int(shot["finalEndMs"]))
        else:
            boundary_out = internal_sides[i]
        entries.append(
            {"shotID": shot["shotID"], "boundaryIn": boundary_in, "boundaryOut": boundary_out}
        )
    return entries


def _confidence_for_score(score: float) -> str:
    return "high" if score >= _HIGH_CONFIDENCE_SCORE else "medium"


def detect_audio_cuts(
    source: Path,
    shots: dict,
    media: dict,
    config: dict,
    *,
    pool: FFmpegPool | None = None,
    align_boundaries: bool = False,
) -> dict:
    """检测音频切点并与视觉边界关联，返回符合 audio-cuts.json 的 dict。

    额外返回键 ``movedBoundaries``（schema 允许附加字段）：align 模式下
    建议移动的 final 边界列表，供 pipeline 更新 shots.json；本函数不改写
    shots dict，detected 边界永不修改。
    """
    source = Path(source)
    audio_cfg = config.get("audioCuts", {})
    sample_rate = int(audio_cfg.get("analysisSampleRate", 16000))
    frame_ms = int(audio_cfg.get("frameMs", 20))
    threshold = float(audio_cfg.get("threshold", 8.0))
    sync_tolerance_ms = int(audio_cfg.get("syncToleranceMs", 100))
    association_window_ms = int(audio_cfg.get("associationWindowMs", 500))

    shot_list = shots.get("shots", [])
    analyzed = media.get("source", {}).get("analyzedRange", {"startMs": 0, "endMs": 0})
    start_ms = int(analyzed["startMs"])
    end_ms = int(analyzed["endMs"])

    analysis = {
        "method": METHOD,
        "algorithmVersion": AUDIO_CUTS_VERSION,
        "analysisSampleRate": sample_rate,
        "frameMs": frame_ms,
        "threshold": threshold,
        "syncToleranceMs": sync_tolerance_ms,
        "associationWindowMs": association_window_ms,
        "selectedBoundaryCount": 0,
    }

    if not media.get("source", {}).get("audioTracks"):
        result = {
            "status": "unavailable",
            "analysis": analysis,
            "boundaries": [],
            "shots": _assemble_shot_entries(shot_list, None),
            "movedBoundaries": [],
        }
        validate_artifact(ArtifactName.AUDIO_CUTS, result)
        return result

    samples = decode_mono_pcm(
        source,
        sample_rate=sample_rate,
        start_ms=start_ms,
        end_ms=end_ms,
        config=config,
        pool=pool,
    )
    features = compute_frame_features(samples, sample_rate=sample_rate, frame_ms=frame_ms)
    window_pairs = max(1, round(_LOCAL_SCALE_WINDOW_MS / frame_ms))
    scores, deltas = novelty_scores(features, local_scale_window_pairs=window_pairs)
    peaks = select_peaks(
        scores,
        frame_ms=frame_ms,
        threshold=threshold,
        min_separation_ms=association_window_ms // 2,
    )

    boundaries: list[dict] = []
    for index, peak in enumerate(peaks, start=1):
        pair_index = peak["pairIndex"]
        time_ms = start_ms + (pair_index + 1) * frame_ms
        score = float(peak["score"])
        boundaries.append(
            {
                "audioBoundaryID": make_audio_boundary_id(index),
                "timeMs": time_ms,
                "score": round(score, 2),
                "confidence": _confidence_for_score(score),
                "featureDeltas": {
                    name: round(float(delta), 4)
                    for name, delta in zip(FEATURE_NAMES, deltas[pair_index])
                },
            }
        )
    analysis["selectedBoundaryCount"] = len(boundaries)

    internal_sides: list[dict] = []
    for i in range(len(shot_list) - 1):
        visual_time = int(shot_list[i]["finalEndMs"])
        internal_sides.append(
            classify_visual_boundary(
                visual_time,
                boundaries,
                sync_tolerance_ms=sync_tolerance_ms,
                association_window_ms=association_window_ms,
            )
        )

    moved: list[dict] = []
    if align_boundaries and internal_sides:
        shots_cfg = config.get("shots", {})
        min_shot_ms = int(shots_cfg.get("minimumShotMs", 500))
        moved = plan_boundary_alignment(
            internal_sides, shot_list, min_shot_ms=min_shot_ms
        )

    result = {
        "status": "complete",
        "analysis": analysis,
        "boundaries": boundaries,
        "shots": _assemble_shot_entries(shot_list, internal_sides),
        "movedBoundaries": moved,
    }
    validate_artifact(ArtifactName.AUDIO_CUTS, result)
    return result
