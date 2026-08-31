"""自适应彩色硬切检测（docs/03 §2.3）。

以源帧率流式解码低分辨率 RGB 帧，用相邻帧 HSV 内容变化作为主信号、
Sobel 边缘变化作为辅助信号。候选阈值来自局部滑动窗口。SSIM 只复核
自适应候选；短段采用置信度感知抑制，并保留在 suppressedBoundaries。
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

import numpy as np

from memoloupe.core.errors import CapabilityUnavailableError
from memoloupe.core.ids import make_shot_id
from memoloupe.media.concurrency import FFmpegPool
from memoloupe.media.proc import ProcessTimeoutError

SHOT_DETECTION_VERSION = "shots.v2"
METHOD = "memoClipHardCutCandidateCuts"

_STDERR_TAIL_BYTES = 4096
_SIZE_RE = re.compile(rb"Video: rawvideo.*rgb24.*?(\d+)x(\d+)")

LIMITATIONS = [
    "Hard cuts are detected adaptively; dissolves and fades require a separate detector.",
    "Boundary times use decoded frame indexes; VFR sources remain approximate.",
    "HSV/Sobel weights, adaptive thresholds and confidence bands are CALIBRATION.",
]


@dataclass(frozen=True)
class FrameFeatures:
    """低分辨率彩色帧特征；数组均为 0..1 float32。"""

    hue: np.ndarray
    saturation: np.ndarray
    value: np.ndarray
    edges: np.ndarray
    luma: np.ndarray
    color_histogram: np.ndarray
    brightness: float


def _rgb_to_hsv(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """无额外图像依赖的向量化 RGB→HSV。"""
    maximum = rgb.max(axis=2)
    minimum = rgb.min(axis=2)
    delta = maximum - minimum
    saturation = np.divide(
        delta, maximum, out=np.zeros_like(delta), where=maximum > 1e-7
    )
    hue = np.zeros_like(maximum)
    active = delta > 1e-7
    red, green, blue = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    mask = active & (maximum == red)
    hue[mask] = ((green[mask] - blue[mask]) / delta[mask]) % 6.0
    mask = active & (maximum == green)
    hue[mask] = (blue[mask] - red[mask]) / delta[mask] + 2.0
    mask = active & (maximum == blue)
    hue[mask] = (red[mask] - green[mask]) / delta[mask] + 4.0
    hue /= 6.0
    return hue, saturation, maximum


def _sobel_edges(luma: np.ndarray) -> np.ndarray:
    padded = np.pad(luma, 1, mode="edge")
    gx = (
        -padded[:-2, :-2] + padded[:-2, 2:]
        - 2.0 * padded[1:-1, :-2] + 2.0 * padded[1:-1, 2:]
        - padded[2:, :-2] + padded[2:, 2:]
    )
    gy = (
        -padded[:-2, :-2] - 2.0 * padded[:-2, 1:-1] - padded[:-2, 2:]
        + padded[2:, :-2] + 2.0 * padded[2:, 1:-1] + padded[2:, 2:]
    )
    return np.clip(np.hypot(gx, gy) / (4.0 * np.sqrt(2.0)), 0.0, 1.0)


def frame_features(frame: bytes, width: int, height: int, bins: int = 254) -> FrameFeatures:
    """计算 RGB 帧的 HSV、Sobel、亮度特征；bins 仅兼容旧调用。"""
    del bins
    expected = width * height * 3
    if len(frame) != expected:
        raise ValueError(f"帧字节数 {len(frame)} 与 RGB24 尺寸 {width}x{height} 不符")
    rgb = np.frombuffer(frame, dtype=np.uint8).reshape(height, width, 3).astype(np.float32)
    rgb /= 255.0
    hue, saturation, value = _rgb_to_hsv(rgb)
    luma = 0.2126 * rgb[:, :, 0] + 0.7152 * rgb[:, :, 1] + 0.0722 * rgb[:, :, 2]
    # 保持稳定字段 histogramSimilarity 的颜色直方图语义：16×4×4 HSV 联合直方图。
    hue_bin = np.minimum((hue * 16).astype(np.int16), 15)
    saturation_bin = np.minimum((saturation * 4).astype(np.int16), 3)
    value_bin = np.minimum((value * 4).astype(np.int16), 3)
    joint_bin = (hue_bin * 4 + saturation_bin) * 4 + value_bin
    color_histogram = np.bincount(joint_bin.ravel(), minlength=256).astype(np.float32)
    color_histogram /= width * height
    return FrameFeatures(
        hue=hue,
        saturation=saturation,
        value=value,
        edges=_sobel_edges(luma),
        luma=luma,
        color_histogram=color_histogram,
        brightness=float(value.mean()),
    )


def pair_metrics(
    prev: FrameFeatures,
    curr: FrameFeatures,
    *,
    histogram_weight: float = 1.0,
    edge_weight: float,
    score_offset: float = 0.0,
) -> dict:
    """计算 HSV 内容变化和 Sobel 结构变化；score 越低越像硬切。"""
    del histogram_weight, score_offset
    hue_diff = np.abs(curr.hue - prev.hue)
    hue_delta = float(np.minimum(hue_diff, 1.0 - hue_diff).mean() * 2.0)
    saturation_delta = float(np.abs(curr.saturation - prev.saturation).mean())
    value_delta = float(np.abs(curr.value - prev.value).mean())
    content_delta = 255.0 * (hue_delta + saturation_delta + value_delta) / 3.0
    edge_delta = 255.0 * float(np.abs(curr.edges - prev.edges).mean())
    change_value = content_delta + edge_weight * edge_delta
    histogram_similarity = float(
        np.minimum(prev.color_histogram, curr.color_histogram).sum()
    )
    return {
        "histogramSimilarity": histogram_similarity,
        "edgeSimilarity": max(0.0, 1.0 - edge_delta / 255.0),
        "brightnessDelta": abs(curr.brightness - prev.brightness),
        "contentDelta": content_delta,
        "edgeDelta": edge_delta,
        "changeValue": change_value,
        "score": -change_value,
    }


def _global_ssim(a: np.ndarray, b: np.ndarray) -> float:
    """低成本全局 SSIM，仅用于临界候选复核。"""
    mean_a, mean_b = float(a.mean()), float(b.mean())
    var_a, var_b = float(a.var()), float(b.var())
    covariance = float(((a - mean_a) * (b - mean_b)).mean())
    c1, c2 = 0.01**2, 0.03**2
    denominator = (mean_a**2 + mean_b**2 + c1) * (var_a + var_b + c2)
    if denominator <= 0:
        return 1.0
    numerator = (2 * mean_a * mean_b + c1) * (2 * covariance + c2)
    return max(-1.0, min(1.0, numerator / denominator))


def select_boundaries(
    changes: list[float],
    *,
    minimum_frames: int,
    adaptive_window: int = 3,
    adaptive_threshold: float = 3.5,
    min_content_value: float = 15.0,
    hard_cut_threshold: float = 45.0,
    rapid_cut_minimum_frames: int = 1,
    ssim_values: list[float | None] | None = None,
    ssim_max_for_adaptive: float = 0.94,
    return_suppressed: bool = False,
    mad_k: float | None = None,
) -> list[dict] | tuple[list[dict], list[dict]]:
    """局部滑窗选择候选，再进行置信度感知的短段合并。"""
    del mad_k
    if len(changes) < 2:
        return ([], []) if return_suppressed else []
    ssim_values = ssim_values or [None] * len(changes)
    candidates: list[dict] = []
    rejected: list[dict] = []
    for pair_index in range(1, len(changes) - 1):
        start = max(0, pair_index - adaptive_window)
        end = min(len(changes), pair_index + adaptive_window + 1)
        context = changes[start:pair_index] + changes[pair_index + 1:end]
        local_mean = sum(context) / len(context) if context else 0.0
        change = changes[pair_index]
        adaptive_ratio = change / max(local_mean, 1.0)
        if change >= hard_cut_threshold:
            reason, confidence = "rawNegativeScore", "high"
        elif change >= min_content_value and adaptive_ratio >= adaptive_threshold:
            reason = "adaptiveOutlier"
            confidence = "medium" if adaptive_ratio >= adaptive_threshold * 1.5 else "low"
        else:
            continue
        candidate = {
            "pairIndex": pair_index,
            "score": -change,
            "changeValue": change,
            "adaptiveRatio": adaptive_ratio,
            "selectionReason": reason,
            "confidence": confidence,
        }
        ssim = ssim_values[pair_index]
        if ssim is not None:
            candidate["ssim"] = ssim
        if reason == "adaptiveOutlier" and ssim is not None and ssim > ssim_max_for_adaptive:
            candidate["suppressionReason"] = "ssimSimilarityRejected"
            rejected.append(candidate)
        else:
            candidates.append(candidate)

    accepted: list[dict] = []
    suppressed = list(rejected)
    for candidate in sorted(candidates, key=lambda item: item["changeValue"], reverse=True):
        conflict = None
        for kept in accepted:
            distance = abs(candidate["pairIndex"] - kept["pairIndex"])
            both_high = candidate["confidence"] == kept["confidence"] == "high"
            required = rapid_cut_minimum_frames if both_high else minimum_frames
            if distance < required:
                conflict = kept
                break
        if conflict is None:
            accepted.append(candidate)
        else:
            candidate["suppressionReason"] = "shortSegmentMerge"
            candidate["mergedIntoPairIndex"] = conflict["pairIndex"]
            suppressed.append(candidate)
    accepted.sort(key=lambda item: item["pairIndex"])
    suppressed.sort(key=lambda item: item["pairIndex"])
    return (accepted, suppressed) if return_suppressed else accepted


def _scaled_size(width: int, height: int, size: int) -> tuple[int, int]:
    out_w = min(size, width)
    out_h = 2 * round(height * out_w / width / 2)
    return out_w, max(2, out_h)


def _append_tail(buffer: bytearray, data: bytes) -> None:
    buffer += data
    if len(buffer) > _STDERR_TAIL_BYTES:
        del buffer[: len(buffer) - _STDERR_TAIL_BYTES]


def _iter_rgb_frames(argv: Sequence[str], timeout_sec: float) -> Iterator[tuple[int, int, bytes]]:
    args = tuple(str(a) for a in argv)
    proc = subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False

    def _kill() -> None:
        nonlocal timed_out
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    watchdog = threading.Timer(timeout_sec, _kill)
    watchdog.start()
    stderr_tail = bytearray()

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        while chunk := proc.stderr.read1(65536):
            _append_tail(stderr_tail, chunk)

    returncode = -1
    try:
        width = height = 0
        assert proc.stderr is not None
        while line := proc.stderr.readline():
            _append_tail(stderr_tail, line)
            match = _SIZE_RE.search(line)
            if match:
                width, height = int(match.group(1)), int(match.group(2))
                break
        if not width:
            proc.wait()
            tail = stderr_tail.decode("utf-8", errors="replace").strip()
            raise CapabilityUnavailableError("ffmpeg", f"无法解析 RGB 帧尺寸 (rc={proc.returncode}): {tail}")
        drain = threading.Thread(target=_drain_stderr, daemon=True)
        drain.start()
        frame_size = width * height * 3
        assert proc.stdout is not None
        while True:
            buffer = bytearray()
            while len(buffer) < frame_size:
                chunk = proc.stdout.read(frame_size - len(buffer))
                if not chunk:
                    break
                buffer += chunk
            if len(buffer) != frame_size:
                break
            yield width, height, bytes(buffer)
        returncode = proc.wait()
        drain.join(timeout=5)
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait()
    if timed_out:
        raise ProcessTimeoutError(args, timeout_sec)
    if returncode != 0:
        tail = stderr_tail.decode("utf-8", errors="replace").strip()
        raise CapabilityUnavailableError("ffmpeg", f"ffmpeg 非零退出 (rc={returncode}): {tail}")


def _candidate_boundary(
    candidate: dict,
    pairs: list[dict],
    brightness: list[float],
    fps: float,
    start_sec: float,
) -> tuple[dict, int]:
    pair_index = candidate["pairIndex"]
    frame_index = pair_index + 1
    metrics = pairs[pair_index]
    boundary = {
        "timeSec": frame_index / fps + start_sec,
        "score": metrics["score"],
        "histogramSimilarity": metrics["histogramSimilarity"],
        "edgeSimilarity": metrics["edgeSimilarity"],
        "brightness": brightness[frame_index],
        "brightnessDelta": metrics["brightnessDelta"],
        "contentDelta": metrics["contentDelta"],
        "edgeDelta": metrics["edgeDelta"],
        "changeValue": metrics["changeValue"],
        "adaptiveRatio": candidate["adaptiveRatio"],
        "frameIndex": frame_index,
        "type": "hardCutCandidate",
        "selectionReason": candidate["selectionReason"],
        "confidence": candidate["confidence"],
    }
    for key in ("ssim", "suppressionReason", "mergedIntoPairIndex"):
        if key in candidate:
            boundary[key] = candidate[key]
    return boundary, round((frame_index / fps + start_sec) * 1000)


def detect_shots(
    source: Path, media: dict, config: dict, *, pool: FFmpegPool | None = None
) -> dict:
    del pool
    source = Path(source)
    src = media["source"]
    shots_cfg = config.get("shots", {})
    ffmpeg_cfg = config.get("ffmpeg", {})
    source_fps = float(src.get("frameRate") or shots_cfg.get("analysisFps", 30.0))
    if bool(shots_cfg.get("fullFrameRate", True)):
        fps = min(source_fps, float(shots_cfg.get("maxAnalysisFps", 60.0)))
    else:
        fps = float(shots_cfg.get("analysisFps", 2.0))
    size = int(shots_cfg.get("analysisSize", 128))
    edge_weight = float(shots_cfg.get("edgeWeight", 0.25))
    minimum_shot_ms = int(shots_cfg.get("minimumShotMs", 500))
    rapid_cut_minimum_ms = int(shots_cfg.get("rapidCutMinimumMs", 200))
    minimum_frames = max(1, round(minimum_shot_ms * fps / 1000.0))
    rapid_frames = max(1, round(rapid_cut_minimum_ms * fps / 1000.0))
    timeout_sec = float(ffmpeg_cfg.get("scanTimeoutSec", 300.0))
    start_ms = int(src["analyzedRange"]["startMs"])
    end_ms = int(src["analyzedRange"]["endMs"])
    start_sec = start_ms / 1000.0

    argv = [
        str(ffmpeg_cfg.get("ffmpegPath", "ffmpeg")), "-hide_banner", "-nostdin", "-v", "info",
        "-ss", f"{start_sec:.3f}", "-i", str(source),
        "-t", f"{(end_ms - start_ms) / 1000.0:.3f}",
        "-vf", f"fps={fps},scale='min({size},iw)':-2",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-an", "-",
    ]
    pairs: list[dict] = []
    lumas: list[np.ndarray] = []
    brightness: list[float] = []
    previous: FrameFeatures | None = None
    sample_width = sample_height = 0
    for width, height, frame in _iter_rgb_frames(argv, timeout_sec):
        sample_width, sample_height = width, height
        current = frame_features(frame, width, height)
        lumas.append(current.luma)
        brightness.append(current.brightness)
        if previous is not None:
            pairs.append(pair_metrics(previous, current, edge_weight=edge_weight))
        previous = current
    if not sample_width:
        sample_width, sample_height = _scaled_size(
            int(src["resolution"]["width"]), int(src["resolution"]["height"]), size
        )

    changes = [item["changeValue"] for item in pairs]
    selection_kwargs = {
        "minimum_frames": minimum_frames,
        "adaptive_window": int(shots_cfg.get("adaptiveWindow", 3)),
        "adaptive_threshold": float(shots_cfg.get("adaptiveThreshold", 3.5)),
        "min_content_value": float(shots_cfg.get("minContentValue", 15.0)),
        "hard_cut_threshold": float(shots_cfg.get("hardCutThreshold", 45.0)),
        "rapid_cut_minimum_frames": rapid_frames,
        "ssim_max_for_adaptive": float(shots_cfg.get("ssimMaxForAdaptive", 0.94)),
        "return_suppressed": True,
    }
    preliminary, preliminary_suppressed = select_boundaries(changes, **selection_kwargs)
    ssim_values: list[float | None] = [None] * len(changes)
    for candidate in [*preliminary, *preliminary_suppressed]:
        index = candidate["pairIndex"]
        ssim_values[index] = _global_ssim(lumas[index], lumas[index + 1])
    selected, suppressed = select_boundaries(
        changes, ssim_values=ssim_values, **selection_kwargs
    )

    boundaries: list[dict] = []
    boundary_ms: list[int] = []
    for candidate in selected:
        boundary, cut_ms = _candidate_boundary(candidate, pairs, brightness, fps, start_sec)
        if start_ms < cut_ms < end_ms:
            boundaries.append(boundary)
            boundary_ms.append(cut_ms)
    suppressed_boundaries = [
        _candidate_boundary(item, pairs, brightness, fps, start_sec)[0]
        for item in suppressed
    ]

    by_ms = dict(zip(boundary_ms, boundaries))
    points = [start_ms, *boundary_ms, end_ms]
    shots: list[dict] = []
    for index, (shot_start, shot_end) in enumerate(zip(points, points[1:]), start=1):
        if shot_start == start_ms:
            boundary_in = {"type": "sourceStart", "confidence": "high", "metric": None}
        else:
            item = by_ms[shot_start]
            boundary_in = {"type": "hardCutCandidate", "confidence": item["confidence"], "metric": {"timeSec": item["timeSec"], "score": item["score"]}}
        if shot_end == end_ms:
            boundary_out = {"type": "sourceEnd", "confidence": "high", "metric": None}
        else:
            item = by_ms[shot_end]
            boundary_out = {"type": "hardCutCandidate", "confidence": item["confidence"], "metric": {"timeSec": item["timeSec"], "score": item["score"]}}
        low_confidence = "low" in (boundary_in["confidence"], boundary_out["confidence"])
        shots.append({
            "shotID": make_shot_id(index), "sequenceIndex": index,
            "detectedStartMs": shot_start, "detectedEndMs": shot_end,
            "finalStartMs": shot_start, "finalEndMs": shot_end,
            "durationMs": shot_end - shot_start,
            "boundaryIn": boundary_in, "boundaryOut": boundary_out,
            "needsReview": (shot_end - shot_start) <= minimum_shot_ms or low_confidence,
        })

    return {
        "analysis": {
            "method": METHOD, "algorithmVersion": SHOT_DETECTION_VERSION,
            "fps": fps, "sourceFps": source_fps,
            "sampleWidth": sample_width, "sampleHeight": sample_height,
            "durationMs": int(src["durationMs"]),
            "rawCandidateCount": len(selected) + len(suppressed),
            "selectedBoundaryCount": len(boundaries),
            "suppressedBoundaryCount": len(suppressed_boundaries),
            "minimumShotMs": minimum_shot_ms,
            "limitations": list(LIMITATIONS),
        },
        "boundaries": boundaries,
        "suppressedBoundaries": suppressed_boundaries,
        "shots": shots,
    }
