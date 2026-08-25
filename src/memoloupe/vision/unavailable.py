"""camera-motion.json 降级产物构造（docs/03 §7 降级矩阵）。

Apple Vision 能力不可用（非 macOS、swiftc 缺失、helper 编译/运行失败）
时产出显式 ``capabilityStatus=unavailable`` 的文件：每个镜头 unknown，
不伪造测量值。供 pipeline 与 :mod:`memoloupe.vision.apple_vision`
失败路径共用（M1 stub 逻辑迁移至此）。
"""

from __future__ import annotations

from memoloupe.vision.protocol import (
    DEFAULT_MAX_FRAMES_PER_SHOT,
    DEFAULT_MAX_IMAGE_DIMENSION,
    DEFAULT_SAMPLE_FPS,
    METHOD,
)


def build_unavailable_camera_motion(
    shots: list[dict], media: dict, config: dict, reason: str
) -> dict:
    """构造 camera-motion.json 降级产物；reason 写入 analysis.note。"""
    vision_cfg = config.get("vision", {}) if isinstance(config, dict) else {}
    duration_ms = int(media.get("source", {}).get("durationMs", 0))
    entries = [
        {
            "shotID": shot["shotID"],
            "sequenceIndex": int(shot["sequenceIndex"]),
            "startMs": int(shot["finalStartMs"]),
            "endMs": int(shot["finalEndMs"]),
            "durationMs": int(shot.get("durationMs", shot["finalEndMs"] - shot["finalStartMs"])),
            "sampleCount": 0,
            "cameraMovement": "unknown",
            "cameraMovementCandidates": ["unknown"],
            "movementIntensity": "unknown",
            "confidence": "unknown",
            "neutralMotions": [],
            "needsReview": True,
            "metrics": {},
            "evidence": {
                "method": f"unavailable（{reason}）",
                "discontinuityFrameIndexes": [],
                "frames": [],
            },
        }
        for shot in shots
    ]
    return {
        "analysis": {
            "method": METHOD,
            "capabilityStatus": "unavailable",
            "durationMs": duration_ms,
            "sampleFps": float(vision_cfg.get("sampleFps", DEFAULT_SAMPLE_FPS)),
            "maximumFramesPerShot": int(
                vision_cfg.get("maximumFramesPerShot", DEFAULT_MAX_FRAMES_PER_SHOT)
            ),
            "maximumImageDimension": int(
                vision_cfg.get("maximumImageDimension", DEFAULT_MAX_IMAGE_DIMENSION)
            ),
            "note": reason,
        },
        "shots": entries,
    }
