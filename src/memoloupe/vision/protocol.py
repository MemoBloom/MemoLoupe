"""Apple Vision helper 协议与镜头级运动聚合（docs/03 §2.11、docs/01 §7.4）。

协议（docs/01 §7.4）：helper 从 stdin 读 JSON 请求，stdout 只输出 JSON 结果。

本模块只含纯函数，不执行任何子进程：

- :func:`build_request` 构造 helper 请求；
- :func:`parse_response` 严格解析 helper 响应（非法结构抛
  :class:`VisionProtocolError`）；
- :func:`aggregate_shot_motion` 把帧级 warp 分解聚合成 camera-motion.json
  的镜头条目（分类字段、置信度、metrics、evidence）。

认识论约定（D-005）：helper 测的是**图像位移**（画面内容在帧间的移动），
不是摄影机运动。画面内容右移对应摄影机左移；分类标签只是候选，
neutralMotions 用中性命名保留原始信号。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from statistics import median

from memoloupe.core.errors import MemoLoupeError

#: docs/07 原文方法名。当前版本仅运行 homography registration，
#: optical flow 未启用（analysis.opticalFlowEnabled=false 表达）。
METHOD = (
    "Apple Vision VNTrackHomographicImageRegistrationRequest"
    " + VNTrackOpticalFlowRequest"
)

#: analysis.units 取值（docs/07 示例原文）。
UNITS = "pixel-equivalent motion at the sampled, preferred-transform image size"


class VisionProtocolError(MemoLoupeError):
    """Apple Vision helper 的请求/响应协议被破坏（非法 JSON、结构不符）。"""


# ---------------------------------------------------------------------------
# CALIBRATION：分类阈值默认值（可用 config["vision"] 同名键覆盖）
# ---------------------------------------------------------------------------

DEFAULT_SAMPLE_FPS = 2.0
DEFAULT_MAX_FRAMES_PER_SHOT = 12
DEFAULT_MAX_IMAGE_DIMENSION = 960

# 样本不足判定：sampleCount 低于该值 → unknown + confidence=unknown
MIN_SAMPLE_COUNT = 3
# 参与分类的最少成功配准对数
MIN_OK_PAIRS = 2
# 计入方向一致率的单对最小位移（低于视为噪声，不参与一致性投票）
NOISE_FLOOR_PX = 0.25
# pan/tilt 候选的中位位移下限（像素）
PAN_MIN_SHIFT_PX = 2.0
TILT_MIN_SHIFT_PX = 2.0
# 轴向主导：主轴中位位移须大于另一轴的该倍数
DOMINANCE_RATIO = 1.5
# 方向一致率阈值：与中位方向同号且超噪声下限的对占比
DIRECTION_CONSISTENCY = 0.7
# scale 单调判定：连续超阈的对占比（zoom_in > ZOOM_MIN_SCALE，zoom_out < 1/阈值）
ZOOM_MIN_SCALE = 1.001
ZOOM_MONOTONIC_RATIO = 0.7
# 几何突变：单对位移超过中位 motionScore 的该倍数且超绝对下限
DISCONTINUITY_FACTOR = 5.0
DISCONTINUITY_MIN_SHIFT_PX = 8.0
# 运动强度（按中位 motionScore，像素）：<1 static，<3 low，<10 medium，否则 high
STATIC_MAX_SCORE = 1.0
INTENSITY_LOW_MAX_SCORE = 3.0
INTENSITY_MEDIUM_MAX_SCORE = 10.0
# 置信度：方向/缩放类候选达到该成功对数为 medium，否则 low
CONFIDENT_OK_PAIRS = 4

_VISION_KEY_DEFAULTS: dict[str, float] = {
    "minSampleCount": float(MIN_SAMPLE_COUNT),
    "minOkPairs": float(MIN_OK_PAIRS),
    "noiseFloorPx": NOISE_FLOOR_PX,
    "panMinShiftPx": PAN_MIN_SHIFT_PX,
    "tiltMinShiftPx": TILT_MIN_SHIFT_PX,
    "dominanceRatio": DOMINANCE_RATIO,
    "directionConsistency": DIRECTION_CONSISTENCY,
    "zoomMinScale": ZOOM_MIN_SCALE,
    "zoomMonotonicRatio": ZOOM_MONOTONIC_RATIO,
    "discontinuityFactor": DISCONTINUITY_FACTOR,
    "discontinuityMinShiftPx": DISCONTINUITY_MIN_SHIFT_PX,
    "staticMaxScore": STATIC_MAX_SCORE,
    "intensityLowMaxScore": INTENSITY_LOW_MAX_SCORE,
    "intensityMediumMaxScore": INTENSITY_MEDIUM_MAX_SCORE,
}


def _thresholds(config: dict) -> dict[str, float]:
    """从 config["vision"] 读取阈值覆盖，缺省用模块 CALIBRATION 常量。"""
    vision = config.get("vision", {}) if isinstance(config, dict) else {}
    merged = dict(_VISION_KEY_DEFAULTS)
    if isinstance(vision, dict):
        for key in merged:
            value = vision.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                merged[key] = float(value)
    return merged


# ---------------------------------------------------------------------------
# 请求 / 响应协议
# ---------------------------------------------------------------------------


def build_request(source: Path | str, shots: list[dict], config: dict) -> dict:
    """构造 helper 请求（docs/01 §7.4）。

    shots 为 shots.json 的镜头条目（final 区间优先，缺省退回 startMs/endMs）。
    """
    vision = config.get("vision", {}) if isinstance(config, dict) else {}
    request_shots = []
    for shot in shots:
        start = shot.get("finalStartMs", shot.get("startMs"))
        end = shot.get("finalEndMs", shot.get("endMs"))
        request_shots.append(
            {
                "shotID": str(shot["shotID"]),
                "startMs": int(start),
                "endMs": int(end),
            }
        )
    return {
        "source": str(source),
        "shots": request_shots,
        "sampleFps": float(vision.get("sampleFps", DEFAULT_SAMPLE_FPS)),
        "maximumFramesPerShot": int(
            vision.get("maximumFramesPerShot", DEFAULT_MAX_FRAMES_PER_SHOT)
        ),
        "maximumImageDimension": int(
            vision.get("maximumImageDimension", DEFAULT_MAX_IMAGE_DIMENSION)
        ),
    }


def _require(condition: bool, path: str, message: str) -> None:
    if not condition:
        raise VisionProtocolError(f"helper 响应非法: {path} {message}")


def _require_number(value: object, path: str) -> float:
    _require(
        isinstance(value, (int, float)) and not isinstance(value, bool),
        path,
        f"必须是数值，实际 {type(value).__name__}",
    )
    _require(math.isfinite(value), path, "必须是有限数值")
    return float(value)


def _require_int(value: object, path: str) -> int:
    _require(
        isinstance(value, int) and not isinstance(value, bool),
        path,
        f"必须是整数，实际 {type(value).__name__}",
    )
    return value


def parse_response(text: str) -> dict:
    """严格解析 helper stdout：单个 JSON 对象，结构不符抛 VisionProtocolError。

    返回归一化后的 dict：{"shots": [{"shotID", "frames": [{frameIndex,
    timeMs, shiftX, shiftY, scale, rotationDegrees, ok}]}]}，数值统一为
    int/float，未知字段不保留（不得静默吞掉：结构错误一律报错）。
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VisionProtocolError(f"helper 响应不是合法 JSON: {exc}") from exc
    _require(isinstance(data, dict), "$", "必须是 JSON 对象")
    shots = data.get("shots")
    _require(isinstance(shots, list), "$.shots", "必须是数组")
    parsed_shots: list[dict] = []
    for si, shot in enumerate(shots):
        path = f"$.shots[{si}]"
        _require(isinstance(shot, dict), path, "必须是对象")
        shot_id = shot.get("shotID")
        _require(
            isinstance(shot_id, str) and bool(shot_id), f"{path}.shotID", "必须是非空字符串"
        )
        frames = shot.get("frames")
        _require(isinstance(frames, list), f"{path}.frames", "必须是数组")
        parsed_frames: list[dict] = []
        for fi, frame in enumerate(frames):
            fpath = f"{path}.frames[{fi}]"
            _require(isinstance(frame, dict), fpath, "必须是对象")
            ok = frame.get("ok")
            _require(isinstance(ok, bool), f"{fpath}.ok", "必须是布尔值")
            parsed_frames.append(
                {
                    "frameIndex": _require_int(frame.get("frameIndex"), f"{fpath}.frameIndex"),
                    "timeMs": _require_int(frame.get("timeMs"), f"{fpath}.timeMs"),
                    "shiftX": _require_number(frame.get("shiftX"), f"{fpath}.shiftX"),
                    "shiftY": _require_number(frame.get("shiftY"), f"{fpath}.shiftY"),
                    "scale": _require_number(frame.get("scale"), f"{fpath}.scale"),
                    "rotationDegrees": _require_number(
                        frame.get("rotationDegrees"), f"{fpath}.rotationDegrees"
                    ),
                    "ok": ok,
                }
            )
        parsed_shots.append({"shotID": shot_id, "frames": parsed_frames})
    return {"shots": parsed_shots}


# ---------------------------------------------------------------------------
# 镜头级聚合（纯函数）
# ---------------------------------------------------------------------------

#: 分类标签 → 中性图像运动信号（中性命名：描述图像现象，不断言摄影机行为）。
_NEUTRAL_MOTIONS: dict[str, list[str]] = {
    "pan_left": ["horizontal_frame_shift"],
    "pan_right": ["horizontal_frame_shift"],
    "tilt_up": ["vertical_frame_shift"],
    "tilt_down": ["vertical_frame_shift"],
    "zoom_in": ["frame_scale_change"],
    "zoom_out": ["frame_scale_change"],
    "handheld": ["irregular_frame_jitter"],
    "discontinuity": ["geometric_discontinuity"],
    "static": [],
    "unknown": [],
}


def _round3(value: float) -> float:
    return round(float(value), 3)


def _direction_consistency(values: list[float], median_value: float, noise_floor: float) -> float:
    """与中位方向同号且幅值超噪声下限的样本占比；中位为 0 时记 0。"""
    if not values or median_value == 0:
        return 0.0
    sign = 1 if median_value > 0 else -1
    hits = sum(1 for v in values if v * sign >= noise_floor)
    return hits / len(values)


def _monotonic_ratio(values: list[float], predicate) -> float:
    if not values:
        return 0.0
    return sum(1 for v in values if predicate(v)) / len(values)


def _intensity(motion_score: float, th: dict[str, float]) -> str:
    if motion_score < th["staticMaxScore"]:
        return "static"
    if motion_score < th["intensityLowMaxScore"]:
        return "low"
    if motion_score < th["intensityMediumMaxScore"]:
        return "medium"
    return "high"


def aggregate_shot_motion(shot_frames: list[dict], config: dict) -> dict:
    """把 helper 帧级数据聚合为 camera-motion.json 的镜头条目字段。

    输入为 :func:`parse_response` 归一化后的某镜头 ``frames`` 数组
    （frames[0] 是 identity 占位，帧 i>=1 携带第 (i-1, i) 对的测量）。
    输出含 sampleCount、cameraMovement（主候选）、cameraMovementCandidates、
    movementIntensity、confidence、neutralMotions、needsReview、metrics、
    evidence。保守优先：不确定就 unknown/discontinuity，不夸大。

    方向语义：shiftX/shiftY 是画面内容位移（图像运动）。内容左移
    （shiftX<0）对应摄影机右移 → pan_right；内容右移 → pan_left。
    内容下移（shiftY>0）对应 tilt_up；内容上移 → tilt_down。
    """
    th = _thresholds(config)
    frames = list(shot_frames)
    sample_count = len(frames)
    # 帧 i>=1 中 ok:true 的项即成功配准对
    ok_pairs = [f for f in frames[1:] if f.get("ok") is True]
    ok_pair_count = len(ok_pairs)

    shifts_x = [float(f["shiftX"]) for f in ok_pairs]
    shifts_y = [float(f["shiftY"]) for f in ok_pairs]
    scales = [float(f["scale"]) for f in ok_pairs]
    rotations = [float(f["rotationDegrees"]) for f in ok_pairs]
    pair_scores = [math.hypot(f["shiftX"], f["shiftY"]) for f in ok_pairs]

    med_x = median(shifts_x) if shifts_x else 0.0
    med_y = median(shifts_y) if shifts_y else 0.0
    med_scale = median(scales) if scales else 1.0
    med_rot = median(rotations) if rotations else 0.0
    motion_score = median(pair_scores) if pair_scores else 0.0

    # 几何突变：单对位移超 5×中位且超绝对下限
    discontinuity_indexes = [
        int(f["frameIndex"])
        for f, score in zip(ok_pairs, pair_scores)
        if score > th["discontinuityFactor"] * motion_score
        and score >= th["discontinuityMinShiftPx"]
    ]

    metrics = {
        "medianFrameShiftX": _round3(med_x),
        "medianFrameShiftY": _round3(med_y),
        "medianScale": _round3(med_scale),
        "medianRotationDegrees": _round3(med_rot),
        "motionScore": _round3(motion_score),
        "geometricMotionScore": _round3(motion_score),
        "homographyFrameCount": float(ok_pair_count),
        "opticalFlowFrameCount": 0.0,
    }
    evidence = {
        "method": METHOD,
        "discontinuityFrameIndexes": discontinuity_indexes,
        "frames": frames,
    }

    def entry(
        movement: str,
        candidates: list[str],
        intensity: str,
        confidence: str,
        needs_review: bool,
    ) -> dict:
        neutral: list[str] = []
        for candidate in candidates:
            for motion in _NEUTRAL_MOTIONS.get(candidate, []):
                if motion not in neutral:
                    neutral.append(motion)
        return {
            "sampleCount": sample_count,
            "cameraMovement": movement,
            "cameraMovementCandidates": candidates,
            "movementIntensity": intensity,
            "confidence": confidence,
            "neutralMotions": neutral,
            "needsReview": needs_review,
            "metrics": metrics,
            "evidence": evidence,
        }

    # 样本不足 → unknown（confidence=unknown，不声称任何结论）
    if (
        sample_count < int(th["minSampleCount"])
        or ok_pair_count < int(th["minOkPairs"])
    ):
        return entry("unknown", ["unknown"], "unknown", "unknown", True)

    # 几何突变为最优先（保守：突变存在时不做方向/缩放断言）
    if discontinuity_indexes:
        return entry(
            "discontinuity",
            ["discontinuity"],
            _intensity(motion_score, th),
            "low",
            True,
        )

    consistency_x = _direction_consistency(shifts_x, med_x, th["noiseFloorPx"])
    consistency_y = _direction_consistency(shifts_y, med_y, th["noiseFloorPx"])
    zoom_in_ratio = _monotonic_ratio(scales, lambda s: s > th["zoomMinScale"])
    zoom_out_ratio = _monotonic_ratio(scales, lambda s: s < 1.0 / th["zoomMinScale"])

    candidates: list[str] = []
    # 水平一致位移 → pan（内容右移=镜头左移）
    if (
        abs(med_x) >= th["panMinShiftPx"]
        and abs(med_x) > th["dominanceRatio"] * abs(med_y)
        and consistency_x > th["directionConsistency"]
    ):
        candidates.append("pan_left" if med_x > 0 else "pan_right")
    # 垂直一致位移 → tilt（内容下移=镜头上移）
    if (
        abs(med_y) >= th["tiltMinShiftPx"]
        and abs(med_y) > th["dominanceRatio"] * abs(med_x)
        and consistency_y > th["directionConsistency"]
    ):
        candidates.append("tilt_up" if med_y > 0 else "tilt_down")
    # scale 单调 → zoom（图像尺度变化候选，不区分光学变焦/后期缩放）
    if zoom_in_ratio >= th["zoomMonotonicRatio"] and med_scale > 1.0:
        candidates.append("zoom_in")
    elif zoom_out_ratio >= th["zoomMonotonicRatio"] and med_scale < 1.0:
        candidates.append("zoom_out")

    if candidates:
        confidence = (
            "medium" if ok_pair_count >= int(CONFIDENT_OK_PAIRS) else "low"
        )
        return entry(
            candidates[0], candidates, _intensity(motion_score, th), confidence, True
        )

    # 信号弱 → static
    if motion_score < th["staticMaxScore"]:
        confidence = "high" if ok_pair_count >= int(CONFIDENT_OK_PAIRS) else "medium"
        return entry("static", ["static"], "static", confidence, confidence != "high")

    # 显著运动但无一致方向/单调缩放：双向一致率都低 → handheld 候选
    if (
        consistency_x < th["directionConsistency"]
        and consistency_y < th["directionConsistency"]
    ):
        return entry(
            "handheld", ["handheld"], _intensity(motion_score, th), "low", True
        )

    # 其余：不确定，不夸大
    return entry("unknown", ["unknown"], _intensity(motion_score, th), "low", True)
