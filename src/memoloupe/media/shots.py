"""视觉硬切候选检测（docs/03 §2.3）。

算法概要：

- ffmpeg 按 ``shots.analysisFps`` 解码、灰度化并缩放到不超过
  ``shots.analysisSize`` 的分析尺寸，裸帧流逐帧从 stdout 读取（流式，
  不整段缓冲）。ffmpeg 8 默认 autorotate，旋转无需额外处理。
- 每帧计算 254 bin 归一化直方图、边缘密度（相邻像素水平+垂直梯度
  均值 / 255）与平均亮度（0..1）。
- 相邻帧：histogramSimilarity 用直方图相交法 sum(min)（CALIBRATION）；
  edgeSimilarity = 1 - min(1, |eA-eB| / max(eA, eB, eps))（CALIBRATION）。
- score = scoreOffset - ((1-histSim)*histogramWeight + (1-edgeSim)*edgeWeight)，
  方向保持"越低越像硬切"。
- 入选路径：rawNegativeScore（score < 0）与 adaptiveOutlier
  （score < median - madK*1.4826*MAD 且偏离至少 ``_MIN_ADAPTIVE_GAP``，
  后者防止 MAD≈0 时压缩噪声被误选；均为 CALIBRATION）。
- minimumFrames 最小间距内多个候选只留 score 最低者；首尾 pair 不作候选。

实际帧尺寸以 ffmpeg stderr 中输出流声明（``Video: rawvideo ..., WxH``）
为准，避免重复实现 scale 滤镜的取整规则；解析失败时回退到按
``min(size,iw)`` 与最近偶数高度推算。

注意：流式解码无法用 :func:`memoloupe.media.proc.run_process`（它会整段
缓冲 stdout），因此这里直接管理 Popen，但保持同样的进程组 + 看门狗
超时纪律。``pool`` 参数为流水线批量调度预留，当前逐进程流式解码未使用。
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Iterator, Sequence

from memoloupe.core.errors import CapabilityUnavailableError
from memoloupe.core.ids import make_shot_id
from memoloupe.core.time_ranges import seconds_to_ms
from memoloupe.media.concurrency import FFmpegPool
from memoloupe.media.proc import ProcessTimeoutError

# 算法版本常量：pipeline 指纹引用，任何公式/阈值改动必须递增。
SHOT_DETECTION_VERSION = "shots.v1"

METHOD = "memoClipHardCutCandidateCuts"

_DEFAULT_MAD_K = 3.0
# CALIBRATION：adaptiveOutlier 要求 score 偏离 median 的最小绝对量，
# 避免 MAD≈0（画面几乎静止）时把编码噪声当切点。
_MIN_ADAPTIVE_GAP = 0.5
# CALIBRATION：confidence 分档阈值。
_CONFIDENCE_HIGH_SCORE = -2.0
_EDGE_EPS = 1e-6
# MAD 一致性修正因子，使阈值对应正态分布下的 k 倍标准差。
_MAD_CONSISTENCY = 1.4826

_STDERR_TAIL_BYTES = 4096
_SIZE_RE = re.compile(rb"Video: rawvideo.*gray.*?(\d+)x(\d+)")

LIMITATIONS = [
    "Only hard-cut candidates are detected; dissolves, fades, overlays "
    "and fast subject motion still require visual confirmation.",
    "Boundary times derive from decode timestamps at the analysis fps; "
    "for VFR sources the frameIndex/fps mapping is approximate.",
    "histogram/edge similarity formulas, adaptive threshold and "
    "confidence bands are CALIBRATION, not stable contract.",
]


@dataclass(frozen=True)
class FrameFeatures:
    """单帧特征：归一化直方图、边缘密度、平均亮度（0..1）。"""

    histogram: tuple[float, ...]
    edge_density: float
    brightness: float


def frame_features(frame: bytes, width: int, height: int, bins: int = 254) -> FrameFeatures:
    """计算灰度帧特征。像素值 v 映射到 bin ``v * bins // 256``。"""
    total = width * height
    if len(frame) != total:
        raise ValueError(f"帧字节数 {len(frame)} 与尺寸 {width}x{height} 不符")

    counts = [0] * bins
    for value in frame:
        counts[(value * bins) >> 8] += 1
    histogram = tuple(c / total for c in counts)

    diff_sum = 0
    diff_count = 0
    for y in range(height):
        row = frame[y * width : (y + 1) * width]
        for a, b in zip(row, row[1:]):
            diff_sum += abs(a - b)
        diff_count += width - 1
    for y in range(height - 1):
        row_a = frame[y * width : (y + 1) * width]
        row_b = frame[(y + 1) * width : (y + 2) * width]
        for a, b in zip(row_a, row_b):
            diff_sum += abs(a - b)
        diff_count += width
    edge_density = (diff_sum / diff_count) / 255.0 if diff_count else 0.0

    brightness = (sum(frame) / total) / 255.0
    return FrameFeatures(histogram=histogram, edge_density=edge_density, brightness=brightness)


def pair_metrics(
    prev: FrameFeatures,
    curr: FrameFeatures,
    *,
    histogram_weight: float,
    edge_weight: float,
    score_offset: float,
) -> dict:
    """相邻帧相似度与硬切 score（CALIBRATION；score 越低越像硬切）。"""
    hist_sim = sum(min(a, b) for a, b in zip(prev.histogram, curr.histogram))
    edge_diff = abs(prev.edge_density - curr.edge_density)
    edge_sim = 1.0 - min(
        1.0, edge_diff / max(prev.edge_density, curr.edge_density, _EDGE_EPS)
    )
    brightness_delta = abs(curr.brightness - prev.brightness)
    score = score_offset - (
        (1.0 - hist_sim) * histogram_weight + (1.0 - edge_sim) * edge_weight
    )
    return {
        "histogramSimilarity": hist_sim,
        "edgeSimilarity": edge_sim,
        "brightnessDelta": brightness_delta,
        "score": score,
    }


def _confidence(score: float) -> str:
    """CALIBRATION：score < -2 high，score < 0 medium，其余 low。"""
    if score < _CONFIDENCE_HIGH_SCORE:
        return "high"
    if score < 0:
        return "medium"
    return "low"


def select_boundaries(
    scores: list[float],
    *,
    minimum_frames: int,
    mad_k: float = _DEFAULT_MAD_K,
) -> list[dict]:
    """从相邻帧 score 序列选出边界候选。

    ``scores[p]`` 对应 frame[p] → frame[p+1] 的变化，边界帧为 p+1。
    返回按 pairIndex 升序的候选列表，每项含
    ``pairIndex``/``score``/``selectionReason``/``confidence``。
    """
    n_pairs = len(scores)
    if n_pairs + 1 < minimum_frames:
        # 视频短于最小镜头帧数：单镜头，无候选。
        return []

    center = median(scores)
    mad = median(abs(s - center) for s in scores)
    threshold = center - mad_k * _MAD_CONSISTENCY * mad

    candidates: list[dict] = []
    # 首尾 pair 不作为候选（范围起止本身已是隐含边界）。
    for p in range(1, n_pairs - 1):
        score = scores[p]
        if score < 0:
            reason = "rawNegativeScore"
        elif score < threshold and (center - score) >= _MIN_ADAPTIVE_GAP:
            reason = "adaptiveOutlier"
        else:
            continue
        candidates.append({"pairIndex": p, "score": score, "selectionReason": reason})

    # 最小帧间距抑制：score 低者优先，间距内的其余候选被吞掉。
    accepted: list[dict] = []
    for cand in sorted(candidates, key=lambda c: c["score"]):
        if all(
            abs(cand["pairIndex"] - kept["pairIndex"]) >= minimum_frames
            for kept in accepted
        ):
            accepted.append(cand)
    accepted.sort(key=lambda c: c["pairIndex"])
    for cand in accepted:
        cand["confidence"] = _confidence(cand["score"])
    return accepted


def _scaled_size(width: int, height: int, size: int) -> tuple[int, int]:
    """scale='min(size,iw)':-2 的回退推算：宽取 min(size,iw)，高取最近偶数。"""
    out_w = min(size, width)
    out_h = 2 * round(height * out_w / width / 2)
    return out_w, max(2, out_h)


def _append_tail(buffer: bytearray, data: bytes) -> None:
    buffer += data
    if len(buffer) > _STDERR_TAIL_BYTES:
        del buffer[: len(buffer) - _STDERR_TAIL_BYTES]


def _iter_gray_frames(
    argv: Sequence[str], timeout_sec: float
) -> Iterator[tuple[int, int, bytes]]:
    """流式产出 (width, height, frame)；逐帧读取，不整段缓冲 stdout。

    实际帧尺寸从 ffmpeg stderr 的输出流声明解析（权威来源）。
    超时由看门狗线程 killpg 强制终止整个进程组。
    """
    args = tuple(str(a) for a in argv)
    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
        while True:
            chunk = proc.stderr.read1(65536)
            if not chunk:
                break
            _append_tail(stderr_tail, chunk)

    try:
        # 头部阶段在主线程逐行读 stderr，直到拿到输出流尺寸；
        # ffmpeg 在产出帧之前先打印 Output/Stream 信息，不会死锁。
        width = height = 0
        assert proc.stderr is not None
        while True:
            line = proc.stderr.readline()
            if not line:
                break
            _append_tail(stderr_tail, line)
            match = _SIZE_RE.search(line)
            if match:
                width, height = int(match.group(1)), int(match.group(2))
                break
        if not width:
            proc.wait()
            tail = stderr_tail.decode("utf-8", errors="replace").strip()
            raise CapabilityUnavailableError(
                "ffmpeg", f"无法从 stderr 解析帧尺寸 (rc={proc.returncode}): {tail}"
            )

        drain = threading.Thread(target=_drain_stderr, daemon=True)
        drain.start()

        frame_size = width * height
        assert proc.stdout is not None
        while True:
            buffer = bytearray()
            while len(buffer) < frame_size:
                chunk = proc.stdout.read(frame_size - len(buffer))
                if not chunk:
                    break
                buffer += chunk
            if len(buffer) != frame_size:
                break  # EOF 或截断帧（进程被终止）
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
        raise CapabilityUnavailableError(
            "ffmpeg", f"ffmpeg 非零退出 (rc={returncode}): {tail}"
        )


def detect_shots(
    source: Path,
    media: dict,
    config: dict,
    *,
    pool: FFmpegPool | None = None,
) -> dict:
    """检测视觉硬切候选，返回符合 shots.json 的 dict。

    M1 无音频对齐：detected 边界与 final 边界完全一致。
    ``pool`` 为批量调度预留；流式解码不经 run_process（见模块 docstring）。
    """
    del pool  # 见模块 docstring
    source = Path(source)
    src = media["source"]
    shots_cfg = config.get("shots", {})
    ffmpeg_cfg = config.get("ffmpeg", {})

    fps = float(shots_cfg.get("analysisFps", 2.0))
    size = int(shots_cfg.get("analysisSize", 128))
    bins = int(shots_cfg.get("histogramBins", 254))
    minimum_frames = int(shots_cfg.get("minimumFrames", 8))
    histogram_weight = float(shots_cfg.get("histogramWeight", 4.61480465))
    edge_weight = float(shots_cfg.get("edgeWeight", 3.75211168))
    score_offset = float(shots_cfg.get("scoreOffset", 5.485968377115124))
    mad_k = float(shots_cfg.get("madK", _DEFAULT_MAD_K))
    timeout_sec = float(ffmpeg_cfg.get("scanTimeoutSec", 300.0))

    start_ms = int(src["analyzedRange"]["startMs"])
    end_ms = int(src["analyzedRange"]["endMs"])
    duration_ms = int(src["durationMs"])
    start_sec = start_ms / 1000.0

    argv = [
        str(ffmpeg_cfg.get("ffmpegPath", "ffmpeg")),
        "-hide_banner",
        "-nostdin",
        "-v", "info",
        # 输入侧 seek：快速且解码时间戳归零，frameIndex/fps + start 即边界时间。
        "-ss", f"{start_sec:.3f}",
        "-i", str(source),
        "-t", f"{(end_ms - start_ms) / 1000.0:.3f}",
        "-vf", f"fps={fps},scale='min({size},iw)':-2",
        "-f", "rawvideo",
        "-pix_fmt", "gray",
        "-an",
        "-",
    ]

    features: list[FrameFeatures] = []
    sample_width = sample_height = 0
    for width, height, frame in _iter_gray_frames(argv, timeout_sec):
        sample_width, sample_height = width, height
        features.append(frame_features(frame, width, height, bins))
    if not sample_width:
        # rc==0 但零帧（极端异常）：按推算尺寸上报，输出单镜头。
        sample_width, sample_height = _scaled_size(
            int(src["resolution"]["width"]), int(src["resolution"]["height"]), size
        )

    pairs = [
        pair_metrics(
            prev,
            curr,
            histogram_weight=histogram_weight,
            edge_weight=edge_weight,
            score_offset=score_offset,
        )
        for prev, curr in zip(features, features[1:])
    ]
    scores = [m["score"] for m in pairs]
    selected = select_boundaries(scores, minimum_frames=minimum_frames, mad_k=mad_k)

    boundaries: list[dict] = []
    boundary_ms: list[int] = []
    for cand in selected:
        pair_index = cand["pairIndex"]
        frame_index = pair_index + 1  # 边界帧 = pair 的后一帧
        time_sec = frame_index / fps + start_sec
        cut_ms = start_ms + seconds_to_ms(frame_index / fps)
        if not (start_ms < cut_ms < end_ms):
            continue
        metrics = pairs[pair_index]
        boundaries.append(
            {
                "timeSec": time_sec,
                "score": metrics["score"],
                "histogramSimilarity": metrics["histogramSimilarity"],
                "edgeSimilarity": metrics["edgeSimilarity"],
                "brightness": features[frame_index].brightness,
                "brightnessDelta": metrics["brightnessDelta"],
                "frameIndex": frame_index,
                "type": "hardCutCandidate",
                "selectionReason": cand["selectionReason"],
                "confidence": cand["confidence"],
            }
        )
        boundary_ms.append(cut_ms)

    by_ms = dict(zip(boundary_ms, boundaries))
    points = [start_ms, *boundary_ms, end_ms]

    min_shot_ms = seconds_to_ms(minimum_frames / fps)
    shots: list[dict] = []
    for index, (shot_start, shot_end) in enumerate(zip(points, points[1:]), start=1):
        if shot_start == start_ms:
            boundary_in = {"type": "sourceStart", "confidence": "high", "metric": None}
        else:
            b = by_ms[shot_start]
            boundary_in = {
                "type": "hardCutCandidate",
                "confidence": b["confidence"],
                "metric": {"timeSec": b["timeSec"], "score": b["score"]},
            }
        if shot_end == end_ms:
            boundary_out = {"type": "sourceEnd", "confidence": "high", "metric": None}
        else:
            b = by_ms[shot_end]
            boundary_out = {
                "type": "hardCutCandidate",
                "confidence": b["confidence"],
                "metric": {"timeSec": b["timeSec"], "score": b["score"]},
            }
        low_confidence = "low" in (boundary_in["confidence"], boundary_out["confidence"])
        shots.append(
            {
                "shotID": make_shot_id(index),
                "sequenceIndex": index,
                "detectedStartMs": shot_start,
                "detectedEndMs": shot_end,
                "finalStartMs": shot_start,
                "finalEndMs": shot_end,
                "durationMs": shot_end - shot_start,
                "boundaryIn": boundary_in,
                "boundaryOut": boundary_out,
                "needsReview": (shot_end - shot_start) < min_shot_ms or low_confidence,
            }
        )

    frame_rate = src.get("frameRate")
    return {
        "analysis": {
            "method": METHOD,
            "algorithmVersion": SHOT_DETECTION_VERSION,
            "fps": fps,
            "sourceFps": float(frame_rate) if frame_rate else fps,
            "sampleWidth": sample_width,
            "sampleHeight": sample_height,
            "durationMs": duration_ms,
            "selectedBoundaryCount": len(boundaries),
            "limitations": list(LIMITATIONS),
        },
        "boundaries": boundaries,
        "shots": shots,
    }
