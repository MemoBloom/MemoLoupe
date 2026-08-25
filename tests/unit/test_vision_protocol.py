"""vision.protocol 单元测试：helper 协议与镜头级运动聚合（docs/03 §2.11）。"""

from __future__ import annotations

import json

import pytest

from memoloupe.vision.protocol import (
    METHOD,
    VisionProtocolError,
    aggregate_shot_motion,
    build_request,
    parse_response,
)

SHOTS = [
    {
        "shotID": "SH0001",
        "sequenceIndex": 1,
        "finalStartMs": 0,
        "finalEndMs": 3203,
        "durationMs": 3203,
    },
    {
        "shotID": "SH0002",
        "sequenceIndex": 2,
        "finalStartMs": 3203,
        "finalEndMs": 6400,
        "durationMs": 3197,
    },
]


def _frames(
    shifts_x: list[float],
    shifts_y: list[float] | None = None,
    scales: list[float] | None = None,
    ok_flags: list[bool] | None = None,
) -> list[dict]:
    """构造 helper 帧序列：frame 0 为 identity，其后每帧携带一对测量。"""
    n = len(shifts_x)
    shifts_y = shifts_y if shifts_y is not None else [0.1] * n
    scales = scales if scales is not None else [1.0] * n
    ok_flags = ok_flags if ok_flags is not None else [True] * n
    frames = [
        {
            "frameIndex": 0,
            "timeMs": 250,
            "shiftX": 0.0,
            "shiftY": 0.0,
            "scale": 1.0,
            "rotationDegrees": 0.0,
            "ok": True,
        }
    ]
    for i in range(n):
        frames.append(
            {
                "frameIndex": i + 1,
                "timeMs": 750 + i * 500,
                "shiftX": shifts_x[i],
                "shiftY": shifts_y[i],
                "scale": scales[i],
                "rotationDegrees": 0.01,
                "ok": ok_flags[i],
            }
        )
    return frames


# ---------------------------------------------------------------------------
# build_request
# ---------------------------------------------------------------------------


def test_build_request_defaults_and_final_range() -> None:
    req = build_request("/tmp/a.mp4", SHOTS, {})
    assert req["source"] == "/tmp/a.mp4"
    assert req["sampleFps"] == 2.0
    assert req["maximumFramesPerShot"] == 12
    assert req["maximumImageDimension"] == 960
    assert req["shots"] == [
        {"shotID": "SH0001", "startMs": 0, "endMs": 3203},
        {"shotID": "SH0002", "startMs": 3203, "endMs": 6400},
    ]


def test_build_request_config_override() -> None:
    config = {
        "vision": {"sampleFps": 4.0, "maximumFramesPerShot": 6, "maximumImageDimension": 640}
    }
    req = build_request("/tmp/a.mp4", SHOTS[:1], config)
    assert req["sampleFps"] == 4.0
    assert req["maximumFramesPerShot"] == 6
    assert req["maximumImageDimension"] == 640


def test_build_request_falls_back_to_plain_range() -> None:
    shots = [{"shotID": "SH0001", "startMs": 100, "endMs": 900}]
    req = build_request("/tmp/a.mp4", shots, {})
    assert req["shots"] == [{"shotID": "SH0001", "startMs": 100, "endMs": 900}]


# ---------------------------------------------------------------------------
# parse_response
# ---------------------------------------------------------------------------


def _response_text(frames: list[dict]) -> str:
    return json.dumps({"shots": [{"shotID": "SH0001", "frames": frames}]})


def test_parse_response_roundtrip() -> None:
    frames = _frames([-8.0] * 5)
    parsed = parse_response(_response_text(frames))
    assert parsed["shots"][0]["shotID"] == "SH0001"
    assert len(parsed["shots"][0]["frames"]) == 6
    assert parsed["shots"][0]["frames"][1]["shiftX"] == -8.0
    assert parsed["shots"][0]["frames"][1]["ok"] is True


def test_parse_response_rejects_invalid_json() -> None:
    with pytest.raises(VisionProtocolError):
        parse_response("not json {")
    with pytest.raises(VisionProtocolError):
        parse_response("[1, 2, 3]")


def test_parse_response_rejects_structure_errors() -> None:
    # 缺 shotID
    with pytest.raises(VisionProtocolError):
        parse_response(json.dumps({"shots": [{"frames": []}]}))
    # frames 非数组
    with pytest.raises(VisionProtocolError):
        parse_response(json.dumps({"shots": [{"shotID": "SH0001", "frames": {}}]}))
    # ok 非布尔
    bad = _frames([-8.0] * 5)
    bad[1]["ok"] = 1
    with pytest.raises(VisionProtocolError):
        parse_response(_response_text(bad))
    # 非有限数值
    bad = _frames([-8.0] * 5)
    bad[1]["shiftX"] = float("nan")
    with pytest.raises(VisionProtocolError):
        parse_response(_response_text(bad))
    # frameIndex 非整数
    bad = _frames([-8.0] * 5)
    bad[1]["frameIndex"] = "1"
    with pytest.raises(VisionProtocolError):
        parse_response(_response_text(bad))


# ---------------------------------------------------------------------------
# aggregate_shot_motion：分类路径
# ---------------------------------------------------------------------------


def test_aggregate_pan_right_consistent_leftward_content_shift() -> None:
    # 画面内容一致左移（shiftX<0）→ 摄影机右移 → pan_right
    result = aggregate_shot_motion(_frames([-8.0] * 5), {})
    assert result["cameraMovement"] == "pan_right"
    assert result["cameraMovementCandidates"] == ["pan_right"]
    assert result["neutralMotions"] == ["horizontal_frame_shift"]
    assert result["movementIntensity"] == "medium"
    assert result["confidence"] == "medium"
    assert result["needsReview"] is True
    assert result["sampleCount"] == 6
    assert result["metrics"]["medianFrameShiftX"] == -8.0
    assert result["metrics"]["homographyFrameCount"] == 5.0
    assert result["metrics"]["opticalFlowFrameCount"] == 0.0
    assert result["evidence"]["method"] == METHOD
    assert result["evidence"]["discontinuityFrameIndexes"] == []


def test_aggregate_pan_left_consistent_rightward_content_shift() -> None:
    result = aggregate_shot_motion(_frames([8.0] * 5), {})
    assert result["cameraMovement"] == "pan_left"


def test_aggregate_tilt_up_down() -> None:
    up = aggregate_shot_motion(_frames([0.1] * 5, shifts_y=[6.0] * 5), {})
    assert up["cameraMovement"] == "tilt_up"
    assert up["neutralMotions"] == ["vertical_frame_shift"]
    down = aggregate_shot_motion(_frames([0.1] * 5, shifts_y=[-6.0] * 5), {})
    assert down["cameraMovement"] == "tilt_down"


def test_aggregate_zoom_in_monotonic_scale() -> None:
    result = aggregate_shot_motion(
        _frames([0.1] * 5, scales=[1.01, 1.011, 1.009, 1.012, 1.01]), {}
    )
    assert result["cameraMovement"] == "zoom_in"
    assert result["neutralMotions"] == ["frame_scale_change"]
    assert result["metrics"]["medianScale"] == pytest.approx(1.01)


def test_aggregate_zoom_out_monotonic_scale() -> None:
    result = aggregate_shot_motion(
        _frames([0.1] * 5, scales=[0.99, 0.991, 0.989, 0.99, 0.992]), {}
    )
    assert result["cameraMovement"] == "zoom_out"


def test_aggregate_handheld_irregular_motion() -> None:
    result = aggregate_shot_motion(
        _frames([5.0, -6.0, 7.0, -5.0, 6.0], shifts_y=[3.0, -4.0, 2.0, -3.0, 4.0]),
        {},
    )
    assert result["cameraMovement"] == "handheld"
    assert result["neutralMotions"] == ["irregular_frame_jitter"]
    assert result["confidence"] == "low"
    assert result["needsReview"] is True


def test_aggregate_discontinuity_spike() -> None:
    result = aggregate_shot_motion(_frames([1.0, 1.0, 50.0, 1.0, 1.0]), {})
    assert result["cameraMovement"] == "discontinuity"
    assert result["cameraMovementCandidates"] == ["discontinuity"]
    assert result["evidence"]["discontinuityFrameIndexes"] == [3]
    assert result["confidence"] == "low"
    assert result["needsReview"] is True


def test_aggregate_static_weak_signal() -> None:
    result = aggregate_shot_motion(
        _frames([0.2, -0.3, 0.1, 0.25, -0.2], shifts_y=[0.1] * 5), {}
    )
    assert result["cameraMovement"] == "static"
    assert result["movementIntensity"] == "static"
    assert result["confidence"] == "high"
    assert result["needsReview"] is False
    assert result["neutralMotions"] == []


def test_aggregate_insufficient_samples_unknown() -> None:
    result = aggregate_shot_motion(_frames([-8.0]), {})
    assert result["sampleCount"] == 2
    assert result["cameraMovement"] == "unknown"
    assert result["cameraMovementCandidates"] == ["unknown"]
    assert result["movementIntensity"] == "unknown"
    assert result["confidence"] == "unknown"
    assert result["needsReview"] is True


def test_aggregate_insufficient_ok_pairs_unknown() -> None:
    # 样本数够但成功配准对不足 → 同样 unknown
    frames = _frames([-8.0] * 5, ok_flags=[True, False, False, False, False])
    result = aggregate_shot_motion(frames, {})
    assert result["cameraMovement"] == "unknown"
    assert result["confidence"] == "unknown"
    assert result["metrics"]["homographyFrameCount"] == 1.0


def test_aggregate_conservative_opposite_directions_not_pan() -> None:
    # 两对方向相反：不得判 pan（保守优先）
    result = aggregate_shot_motion(_frames([-8.0, 8.0]), {})
    assert not result["cameraMovement"].startswith("pan")
    assert not result["cameraMovement"].startswith("tilt")
    assert result["cameraMovement"] == "handheld"


def test_aggregate_config_threshold_override() -> None:
    # 提高 pan 阈值后，同样证据不得判 pan（阈值可经 config["vision"] 覆盖）
    config = {"vision": {"panMinShiftPx": 20.0}}
    result = aggregate_shot_motion(_frames([-8.0] * 5), config)
    assert result["cameraMovement"] != "pan_right"


def test_aggregate_failed_pairs_excluded_from_medians() -> None:
    frames = _frames([-8.0, -8.0, -8.0, -8.0, 999.0], ok_flags=[True] * 4 + [False])
    result = aggregate_shot_motion(frames, {})
    assert result["metrics"]["medianFrameShiftX"] == -8.0
    assert result["metrics"]["homographyFrameCount"] == 4.0
    assert result["evidence"]["discontinuityFrameIndexes"] == []


def test_aggregate_combined_pan_and_zoom_candidates() -> None:
    # 水平一致位移 + 单调缩放：两个候选都保留，主候选为 pan
    result = aggregate_shot_motion(
        _frames([-8.0] * 5, scales=[1.01] * 5), {}
    )
    assert result["cameraMovement"] == "pan_right"
    assert set(result["cameraMovementCandidates"]) == {"pan_right", "zoom_in"}
    assert result["neutralMotions"] == ["horizontal_frame_shift", "frame_scale_change"]
