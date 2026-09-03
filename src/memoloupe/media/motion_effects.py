"""运动复刻候选检测（L7 absorption v1）。

本模块输出 ``raw/motion-effects.json``：它是后期动效复刻的确定性候选证据，
不是剪辑工程真值。所有 speed/keyframe 候选都固定
``needsVisualConfirmation=true``，由人工视觉复核决定是否采用。
"""

from __future__ import annotations

from pathlib import Path
from statistics import mean
from typing import Any

import numpy as np

from memoloupe.artifacts.schemas import ArtifactName, validate_artifact

MOTION_EFFECTS_VERSION = "motion-effects.v1"
METHOD = "frameDifferenceMotionEffectCandidates"

SAMPLE_WIDTH = 96
SAMPLE_HEIGHT = 54
MAX_SHIFT_PX = 8
SCALE_HYPOTHESES = (0.94, 0.97, 1.0, 1.03, 1.06)

LIMITATIONS = [
    "Motion effect candidates are inferred from final pixels and are not edit-project truth.",
    "The detector cannot reliably separate camera motion, subject motion, and post-production transforms.",
    "Small local overlays, rotation, compression noise, motion blur, and very fast cuts require visual review.",
]


def _round3(value: float) -> float:
    return round(float(value), 3)


def _round4(value: float) -> float:
    return round(float(value), 4)


def _percentile(values: list[float], q: float, default: float = 0.0) -> float:
    if not values:
        return default
    return float(np.percentile(np.asarray(values, dtype=np.float32), q))


def _overlap_slices(dx: int, dy: int, width: int, height: int) -> tuple[slice, slice, slice, slice]:
    if dx >= 0:
        prev_x = slice(0, width - dx)
        cur_x = slice(dx, width)
    else:
        prev_x = slice(-dx, width)
        cur_x = slice(0, width + dx)
    if dy >= 0:
        prev_y = slice(0, height - dy)
        cur_y = slice(dy, height)
    else:
        prev_y = slice(-dy, height)
        cur_y = slice(0, height + dy)
    return prev_y, prev_x, cur_y, cur_x


def estimate_translation(
    prev: np.ndarray,
    cur: np.ndarray,
    *,
    max_shift_px: int = MAX_SHIFT_PX,
) -> tuple[int, int, float]:
    """暴力块匹配估计全局平移，返回 ``(dx, dy, residual)``。"""
    height, width = prev.shape
    margin = min(10, max(0, min(height, width) // 6))
    if height <= margin * 2 + 1 or width <= margin * 2 + 1:
        a = prev
        b = cur
    else:
        a = prev[margin:-margin, margin:-margin]
        b = cur[margin:-margin, margin:-margin]
    h, w = a.shape
    best: tuple[int, int, float] | None = None
    for dy in range(-max_shift_px, max_shift_px + 1):
        for dx in range(-max_shift_px, max_shift_px + 1):
            if abs(dx) >= w or abs(dy) >= h:
                continue
            py, px, cy, cx = _overlap_slices(dx, dy, w, h)
            residual = float(np.mean(np.abs(a[py, px] - b[cy, cx])))
            if best is None or residual < best[2]:
                best = (dx, dy, residual)
    return best if best is not None else (0, 0, float(np.mean(np.abs(prev - cur))))


def _resize_nearest(image: np.ndarray, new_h: int, new_w: int) -> np.ndarray:
    src_h, src_w = image.shape
    y_idx = np.clip((np.arange(new_h) * src_h / max(new_h, 1)).astype(int), 0, src_h - 1)
    x_idx = np.clip((np.arange(new_w) * src_w / max(new_w, 1)).astype(int), 0, src_w - 1)
    return image[y_idx[:, None], x_idx[None, :]]


def _scale_to_canvas(image: np.ndarray, scale: float) -> np.ndarray:
    height, width = image.shape
    new_h = max(1, round(height * scale))
    new_w = max(1, round(width * scale))
    resized = _resize_nearest(image, new_h, new_w)
    canvas = np.zeros_like(image)

    if new_h <= height:
        src_y0, dst_y0, copy_h = 0, (height - new_h) // 2, new_h
    else:
        src_y0, dst_y0, copy_h = (new_h - height) // 2, 0, height
    if new_w <= width:
        src_x0, dst_x0, copy_w = 0, (width - new_w) // 2, new_w
    else:
        src_x0, dst_x0, copy_w = (new_w - width) // 2, 0, width

    canvas[dst_y0:dst_y0 + copy_h, dst_x0:dst_x0 + copy_w] = resized[
        src_y0:src_y0 + copy_h, src_x0:src_x0 + copy_w
    ]
    return canvas


def estimate_scale(prev: np.ndarray, cur: np.ndarray) -> tuple[float, float]:
    """五档离散缩放假设检验，返回 ``(scale_ratio, residual)``。"""
    height, width = prev.shape
    margin = min(8, max(0, min(height, width) // 7))
    crop = (
        (slice(margin, height - margin), slice(margin, width - margin))
        if height > margin * 2 + 1 and width > margin * 2 + 1
        else (slice(None), slice(None))
    )
    best: tuple[float, float] | None = None
    for scale in SCALE_HYPOTHESES:
        scaled = _scale_to_canvas(prev, scale)
        residual = float(np.mean(np.abs(scaled[crop] - cur[crop])))
        if best is None or residual < best[1]:
            best = (scale, residual)
    return best if best is not None else (1.0, float(np.mean(np.abs(prev - cur))))


def _frame_metrics(frames: np.ndarray, fps: float, start_ms: int) -> list[dict]:
    metrics: list[dict] = []
    prev_dx = 0.0
    prev_dy = 0.0
    for index in range(1, len(frames)):
        prev = frames[index - 1]
        cur = frames[index]
        diff_map = np.abs(cur - prev)
        diff = float(np.mean(diff_map))
        gy, gx = np.gradient(cur)
        edge = np.hypot(gx, gy)
        motion = float(np.mean(diff_map * (0.35 + edge)))
        brightness = float(np.mean(cur))
        prev_brightness = float(np.mean(prev))
        brightness_delta = brightness - prev_brightness
        repeat_score = 1.0 - min(1.0, diff / 0.25)
        cut_score = diff + 0.7 * abs(brightness_delta)
        dx, dy, trans_score = estimate_translation(prev, cur)
        scale, scale_score = estimate_scale(prev, cur)
        zoom_score = abs(scale - 1.0) + max(0.0, trans_score - scale_score)
        shake_score = abs(float(dx) - prev_dx) + abs(float(dy) - prev_dy)
        prev_dx = float(dx)
        prev_dy = float(dy)
        metrics.append(
            {
                "frameIndex": index,
                "timeMs": start_ms + round(index * 1000 / fps),
                "diff": _round4(diff),
                "motionEnergy": _round4(motion),
                "brightness": _round4(brightness),
                "brightnessDelta": _round4(brightness_delta),
                "repeatScore": _round4(repeat_score),
                "cutScore": _round4(cut_score),
                "dxPxSample": _round3(dx),
                "dyPxSample": _round3(dy),
                "scaleRatio": _round4(scale),
                "zoomScore": _round4(zoom_score),
                "shakeScore": _round3(shake_score),
                "translationResidual": _round4(trans_score),
                "scaleResidual": _round4(scale_score),
            }
        )
    return metrics


def _thresholds(metrics: list[dict]) -> dict[str, float]:
    motions = [float(m["motionEnergy"]) for m in metrics]
    cut_scores = [float(m["cutScore"]) for m in metrics]
    translations = [
        float(np.hypot(float(m["dxPxSample"]), float(m["dyPxSample"])))
        for m in metrics
    ]
    zooms = [float(m["zoomScore"]) for m in metrics]
    shakes = [float(m["shakeScore"]) for m in metrics]
    brightness = [abs(float(m["brightnessDelta"])) for m in metrics]
    return {
        "motionP20": _round4(_percentile(motions, 20)),
        "motionP50": _round4(_percentile(motions, 50)),
        "motionP80": _round4(_percentile(motions, 80)),
        "cutP90": _round4(_percentile(cut_scores, 90)),
        "translationP90": _round4(_percentile(translations, 90)),
        "zoomP90": _round4(_percentile(zooms, 90)),
        "shakeP90": _round4(_percentile(shakes, 90)),
        "brightnessDeltaP92": _round4(_percentile(brightness, 92)),
        "repeatFreezeMin": 0.78,
        "brightnessDeltaFloor": 0.12,
        "impactBrightnessFloor": 0.18,
        "translationFloorPx": 2.0,
        "zoomFloor": 0.025,
        "shakeFloorPx": 2.5,
    }


def group_indices(indices: list[int], *, max_gap: int = 2) -> list[list[int]]:
    """把相邻命中帧分组；组内相邻 index 间隔不超过 ``max_gap``。"""
    if not indices:
        return []
    groups: list[list[int]] = [[indices[0]]]
    for index in indices[1:]:
        if index - groups[-1][-1] <= max_gap:
            groups[-1].append(index)
        else:
            groups.append([index])
    return groups


def _confidence(value: float, threshold: float) -> str:
    if threshold <= 0:
        return "unknown"
    ratio = value / threshold
    if ratio >= 1.8:
        return "high"
    if ratio >= 1.25:
        return "medium"
    return "low"


def _ref(metric_index: int) -> str:
    return f"raw/motion-effects.json#frameMetrics[{metric_index}]"


def _metric_range(metrics: list[dict], group: list[int], fps: float) -> tuple[int, int]:
    start_ms = int(metrics[group[0]]["timeMs"])
    end_ms = int(metrics[group[-1]]["timeMs"]) + round(1000 / fps)
    return start_ms, max(end_ms, start_ms)


def detect_speed_ramps(metrics: list[dict], thresholds: dict[str, float], fps: float) -> list[dict]:
    """检测区域事件：低运动/冻结、高运动区与冲击卡点。"""
    if not metrics:
        return []
    min_region = max(1, round(fps * 0.25))
    motion_p20 = thresholds["motionP20"]
    motion_p50 = thresholds["motionP50"]
    motion_p80 = thresholds["motionP80"]
    cut_threshold = thresholds["cutP90"]
    events: list[dict] = []

    low_hits = [
        i for i, m in enumerate(metrics)
        if float(m["motionEnergy"]) <= motion_p20 and float(m["repeatScore"]) > 0.78
    ]
    high_hits = [
        i for i, m in enumerate(metrics)
        if float(m["motionEnergy"]) >= motion_p80
    ]
    impact_hits = [
        i for i, m in enumerate(metrics)
        if (
            (float(m["cutScore"]) >= cut_threshold
             or abs(float(m["brightnessDelta"])) > thresholds["impactBrightnessFloor"])
            and float(m["motionEnergy"]) >= motion_p50
        )
    ]

    for event_type, hits, hint in (
        (
            "low_motion_or_freeze",
            low_hits,
            "Consider a short hold, freeze beat, or slow-motion section; confirm by watching action speed.",
        ),
        (
            "high_motion_region",
            high_hits,
            "Treat as rhythm density or motion emphasis; do not label fast-forward without visual confirmation.",
        ),
    ):
        for group in group_indices(hits):
            if len(group) < min_region:
                continue
            start_ms, end_ms = _metric_range(metrics, group, fps)
            avg_motion = mean(float(metrics[i]["motionEnergy"]) for i in group)
            threshold = motion_p20 if event_type == "low_motion_or_freeze" else motion_p80
            confidence_value = (threshold / avg_motion) if event_type == "low_motion_or_freeze" and avg_motion > 0 else avg_motion
            confidence_threshold = 1.0 if event_type == "low_motion_or_freeze" else threshold
            events.append(
                {
                    "type": event_type,
                    "startMs": start_ms,
                    "endMs": end_ms,
                    "durationMs": end_ms - start_ms,
                    "avgMotion": _round4(avg_motion),
                    "confidence": _confidence(confidence_value, confidence_threshold),
                    "evidence": f"{event_type}: {len(group)} sampled frame pairs",
                    "replicationHint": hint,
                    "needsVisualConfirmation": True,
                    "evidenceRefs": [_ref(group[0]), _ref(group[-1])],
                }
            )

    for group in group_indices(impact_hits, max_gap=1):
        peak = max(group, key=lambda i: float(metrics[i]["cutScore"]))
        start_ms, end_ms = _metric_range(metrics, group, fps)
        peak_score = float(metrics[peak]["cutScore"])
        events.append(
            {
                "type": "impact_cut",
                "startMs": start_ms,
                "endMs": end_ms,
                "durationMs": end_ms - start_ms,
                "avgMotion": _round4(mean(float(metrics[i]["motionEnergy"]) for i in group)),
                "confidence": _confidence(peak_score, cut_threshold),
                "evidence": f"cut_score peak={peak_score:.4f}",
                "replicationHint": "Use a 2-4 frame exposure/opacity hit, optionally paired with short shake decay.",
                "needsVisualConfirmation": True,
                "evidenceRefs": [_ref(peak)],
            }
        )
    return events


def _shot_for_time(shots: list[dict], time_ms: int) -> dict | None:
    for shot in shots:
        start = shot.get("finalStartMs")
        end = shot.get("finalEndMs")
        if isinstance(start, int) and isinstance(end, int) and start <= time_ms < end:
            return shot
    return shots[-1] if shots and isinstance(shots[-1].get("finalEndMs"), int) and time_ms >= shots[-1]["finalEndMs"] else None


def _source_scale(media: dict) -> float:
    width = media.get("source", {}).get("resolution", {}).get("width")
    return float(width) / SAMPLE_WIDTH if isinstance(width, int) and width > 0 else 1.0


def detect_keyframes(
    metrics: list[dict],
    thresholds: dict[str, float],
    shots: list[dict],
    media: dict,
) -> list[dict]:
    """检测点事件：position、scale、exposure/opacity 与 shake 候选。"""
    if not metrics:
        return []
    cut_guard = thresholds["cutP90"]
    source_scale = _source_scale(media)
    specs = [
        (
            "position",
            lambda m: float(np.hypot(float(m["dxPxSample"]), float(m["dyPxSample"]))),
            max(thresholds["translationFloorPx"], thresholds["translationP90"]),
            "Use position keyframes with easing; confirm this is not subject/camera motion.",
        ),
        (
            "scale",
            lambda m: float(m["zoomScore"]),
            max(thresholds["zoomFloor"], thresholds["zoomP90"]),
            "Use scale keyframes with easing; confirm the zoom hypothesis visually.",
        ),
        (
            "shake",
            lambda m: float(m["shakeScore"]),
            max(thresholds["shakeFloorPx"], thresholds["shakeP90"]),
            "Use short position shake with exponential decay.",
        ),
    ]
    candidates: list[dict] = []
    for prop, metric_fn, threshold, hint in specs:
        hits = [
            i for i, m in enumerate(metrics)
            if metric_fn(m) >= threshold and float(m["cutScore"]) < cut_guard
        ]
        for group in group_indices(hits):
            peak = max(group, key=lambda i: metric_fn(metrics[i]))
            metric = metrics[peak]
            shot = _shot_for_time(shots, int(metric["timeMs"]))
            if shot is None:
                continue
            dx = float(metric["dxPxSample"])
            dy = float(metric["dyPxSample"])
            scale = float(metric["scaleRatio"])
            change: dict[str, Any] = {
                "text": _inferred_change_text(prop, dx, dy, scale, 0.0, source_scale),
                "sampleDxPx": _round3(dx),
                "sampleDyPx": _round3(dy),
                "sourceDxPxEstimate": _round3(dx * source_scale),
                "sourceDyPxEstimate": _round3(dy * source_scale),
                "sampleScaleRatio": _round4(scale),
            }
            candidates.append(
                {
                    "shotID": shot["shotID"],
                    "timeMs": int(metric["timeMs"]),
                    "property": prop,
                    "inferredChange": change,
                    "confidence": _confidence(metric_fn(metric), threshold),
                    "replicationHint": hint,
                    "needsVisualConfirmation": True,
                    "evidenceRefs": [_ref(peak)],
                }
            )

    exposure_threshold = max(thresholds["brightnessDeltaFloor"], thresholds["brightnessDeltaP92"])
    exposure_hits = [
        i for i, m in enumerate(metrics)
        if abs(float(m["brightnessDelta"])) >= exposure_threshold
    ]
    for group in group_indices(exposure_hits, max_gap=1):
        peak = max(group, key=lambda i: abs(float(metrics[i]["brightnessDelta"])))
        metric = metrics[peak]
        shot = _shot_for_time(shots, int(metric["timeMs"]))
        if shot is None:
            continue
        delta = float(metric["brightnessDelta"])
        candidates.append(
            {
                "shotID": shot["shotID"],
                "timeMs": int(metric["timeMs"]),
                "property": "exposure_or_opacity",
                "inferredChange": {
                    "text": _inferred_change_text("exposure_or_opacity", 0.0, 0.0, 1.0, delta, source_scale),
                    "brightnessDelta": _round4(delta),
                },
                "confidence": _confidence(abs(delta), exposure_threshold),
                "replicationHint": "Use a 2-4 frame exposure bump, white solid, black dip, or opacity flash.",
                "needsVisualConfirmation": True,
                "evidenceRefs": [_ref(peak)],
            }
        )
    return sorted(candidates, key=lambda item: (item["timeMs"], item["property"]))


def _inferred_change_text(
    prop: str,
    dx: float,
    dy: float,
    scale: float,
    brightness_delta: float,
    source_scale: float,
) -> str:
    if prop == "position":
        return (
            f"Position approx ({dx:.1f}px, {dy:.1f}px) in 96x54 sample; "
            f"source estimate ({dx * source_scale:.1f}px, {dy * source_scale:.1f}px)"
        )
    if prop == "scale":
        return f"Scale approx 100% -> {scale * 100:.0f}% over sampled frames"
    if prop == "shake":
        return f"Shake acceleration approx {abs(dx):.1f}px/{abs(dy):.1f}px sample-space impulse"
    direction = "white/exposure rise" if brightness_delta > 0 else "black/exposure dip"
    return f"{direction}: brightness delta {brightness_delta:+.3f}"


def build_digest(speed_ramps: list[dict], keyframes: list[dict]) -> dict:
    items: list[dict] = []
    for event in speed_ramps:
        items.append(
            {
                "kind": str(event["type"]),
                "timeRange": f"{event['startMs']}–{event['endMs']} ms",
                "summary": str(event["evidence"]),
                "confidence": event["confidence"],
                "needsVisualConfirmation": True,
                "evidenceRefs": event["evidenceRefs"],
            }
        )
    for event in keyframes:
        items.append(
            {
                "kind": str(event["property"]),
                "timeRange": f"{event['timeMs']} ms",
                "summary": str(event["inferredChange"]["text"]),
                "confidence": event["confidence"],
                "needsVisualConfirmation": True,
                "evidenceRefs": event["evidenceRefs"],
            }
        )
    priority = {"high": 0, "medium": 1, "low": 2, "unknown": 3}
    items.sort(key=lambda item: (priority.get(str(item["confidence"]), 9), item["timeRange"]))
    return {
        "schemaVersion": 1,
        "items": items[:12],
        "usageNote": "All entries are motion replication candidates from final-pixel evidence and require visual confirmation.",
    }


def _shots_summary(shots: list[dict], keyframes: list[dict]) -> list[dict]:
    by_shot: dict[str, list[dict]] = {}
    for item in keyframes:
        by_shot.setdefault(str(item["shotID"]), []).append(item)
    summaries: list[dict] = []
    for shot in shots:
        shot_id = str(shot["shotID"])
        items = by_shot.get(shot_id, [])
        props = sorted({str(item["property"]) for item in items})
        summaries.append(
            {
                "shotID": shot_id,
                "startMs": int(shot["finalStartMs"]),
                "endMs": int(shot["finalEndMs"]),
                "candidateCount": len(items),
                "properties": props,
                "needsReview": bool(items),
            }
        )
    return summaries


def _read_sample_frames(source: Path, media: dict, config: dict, *, pool) -> np.ndarray:
    ffmpeg = config["ffmpeg"]["ffmpegPath"]
    motion_cfg = config["motionEffects"]
    fps = float(motion_cfg["sampleFps"])
    source_doc = media.get("source", {})
    arange = source_doc.get("analyzedRange", {})
    start_ms = int(arange.get("startMs", 0))
    end_ms = int(arange.get("endMs", source_doc.get("durationMs", 0)))
    argv = [ffmpeg, "-hide_banner"]
    if start_ms > 0:
        argv.extend(["-ss", f"{start_ms / 1000:.3f}"])
    if end_ms > start_ms:
        argv.extend(["-t", f"{(end_ms - start_ms) / 1000:.3f}"])
    argv.extend(
        [
            "-i", str(source),
            "-vf", f"fps={fps:g},scale={SAMPLE_WIDTH}:{SAMPLE_HEIGHT},format=gray",
            "-an", "-f", "rawvideo", "-",
        ]
    )
    result = pool.run(argv, timeout_sec=float(config["ffmpeg"]["scanTimeoutSec"]))
    frame_size = SAMPLE_WIDTH * SAMPLE_HEIGHT
    usable = len(result.stdout) // frame_size * frame_size
    if usable == 0:
        return np.empty((0, SAMPLE_HEIGHT, SAMPLE_WIDTH), dtype=np.float32)
    raw = np.frombuffer(result.stdout[:usable], dtype=np.uint8)
    return raw.reshape((-1, SAMPLE_HEIGHT, SAMPLE_WIDTH)).astype(np.float32) / 255.0


def build_motion_effects_stub(shots: list[dict], media: dict, config: dict) -> dict:
    source_doc = media.get("source", {})
    arange = source_doc.get("analyzedRange", {"startMs": 0, "endMs": 0})
    result = {
        "schemaVersion": 1,
        "status": "skipped",
        "analysis": {
            "method": METHOD,
            "algorithmVersion": MOTION_EFFECTS_VERSION,
            "sourceRevisionID": source_doc.get("revisionID"),
            "durationMs": int(source_doc.get("durationMs", 0)),
            "analyzedRange": {
                "startMs": int(arange.get("startMs", 0)),
                "endMs": int(arange.get("endMs", 0)),
            },
            "sampleFps": float(config.get("motionEffects", {}).get("sampleFps", 8.0)),
            "sampleWidth": SAMPLE_WIDTH,
            "sampleHeight": SAMPLE_HEIGHT,
            "frameCount": 0,
            "thresholds": {},
            "limitations": LIMITATIONS,
            "note": "运动复刻候选检测未运行（--skip）",
        },
        "frameMetrics": [],
        "speedRamps": [],
        "keyframeCandidates": [],
        "digest": {
            "schemaVersion": 1,
            "items": [],
            "usageNote": "Motion effect detection was skipped; no absence conclusion is implied.",
        },
        "shots": [
            {
                "shotID": shot["shotID"],
                "startMs": int(shot["finalStartMs"]),
                "endMs": int(shot["finalEndMs"]),
                "candidateCount": 0,
                "properties": [],
                "needsReview": False,
            }
            for shot in shots
        ],
    }
    validate_artifact(ArtifactName.MOTION_EFFECTS, result)
    return result


def detect_motion_effects(source: Path, shots: list[dict], media: dict, config: dict, *, pool) -> dict:
    """扫描全轨像素运动，输出运动复刻候选契约。"""
    source_doc = media.get("source", {})
    arange = source_doc.get("analyzedRange", {})
    start_ms = int(arange.get("startMs", 0))
    end_ms = int(arange.get("endMs", source_doc.get("durationMs", 0)))
    fps = float(config["motionEffects"]["sampleFps"])
    frames = _read_sample_frames(Path(source), media, config, pool=pool)
    metrics = _frame_metrics(frames, fps, start_ms)
    thresholds = _thresholds(metrics)
    speed_ramps = detect_speed_ramps(metrics, thresholds, fps)
    keyframes = detect_keyframes(metrics, thresholds, shots, media)
    result = {
        "schemaVersion": 1,
        "status": "complete",
        "analysis": {
            "method": METHOD,
            "algorithmVersion": MOTION_EFFECTS_VERSION,
            "sourceRevisionID": source_doc.get("revisionID"),
            "durationMs": int(source_doc.get("durationMs", 0)),
            "analyzedRange": {"startMs": start_ms, "endMs": end_ms},
            "sampleFps": fps,
            "sampleWidth": SAMPLE_WIDTH,
            "sampleHeight": SAMPLE_HEIGHT,
            "frameCount": int(len(frames)),
            "thresholds": thresholds,
            "limitations": LIMITATIONS,
        },
        "frameMetrics": metrics,
        "speedRamps": speed_ramps,
        "keyframeCandidates": keyframes,
        "digest": build_digest(speed_ramps, keyframes),
        "shots": _shots_summary(shots, keyframes),
    }
    validate_artifact(ArtifactName.MOTION_EFFECTS, result)
    return result
