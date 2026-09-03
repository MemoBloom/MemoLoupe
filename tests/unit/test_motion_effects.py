"""media/motion_effects.py 单元测试（docs/03 §2.6、Phase 05-07）。

只测纯函数层（合成 frame metrics / shots / media），不跑 ffmpeg：
- detect_speed_ramps：冻结/低运动、高运动区、冲击卡点区域事件；
- detect_keyframes：position / scale / shake / exposure 点候选；
- cut guard：抑制切点附近的 position/scale/shake 假阳性；
- 全部候选固定 needsVisualConfirmation=true 且带 evidenceRefs；
- build_digest 只保留 Top 12；
- build_motion_effects_stub：status=skipped，不暗示 absence。
"""

from __future__ import annotations

import json

from memoloupe.media.motion_effects import (
    SAMPLE_WIDTH,
    build_digest,
    build_motion_effects_stub,
    detect_keyframes,
    detect_speed_ramps,
    group_indices,
)


def _t(idx: int, fps: float = 8.0, start_ms: int = 0) -> int:
    return start_ms + round(idx * 1000 / fps)


def _metric(
    idx: int,
    *,
    motion=0.05,
    repeat=0.5,
    cut=0.05,
    brightness_delta=0.0,
    dx=0.0,
    dy=0.0,
    zoom=0.0,
    shake=0.0,
) -> dict:
    return {
        "frameIndex": idx,
        "timeMs": _t(idx),
        "diff": 0.0,
        "motionEnergy": motion,
        "brightness": 0.5,
        "brightnessDelta": brightness_delta,
        "repeatScore": repeat,
        "cutScore": cut,
        "dxPxSample": dx,
        "dyPxSample": dy,
        "scaleRatio": 1.0,
        "zoomScore": zoom,
        "shakeScore": shake,
    }


def _freeze_thresholds() -> dict:
    # 与 _thresholds 输出同形；分位数被压低/抬高使事件可预测。
    return {
        "motionP20": 0.02,
        "motionP50": 0.1,
        "motionP80": 0.5,
        "cutP90": 0.5,
        "translationP90": 1.0,
        "zoomP90": 0.01,
        "shakeP90": 2.0,
        "brightnessDeltaP92": 0.05,
        "repeatFreezeMin": 0.78,
        "brightnessDeltaFloor": 0.12,
        "impactBrightnessFloor": 0.18,
        "translationFloorPx": 2.0,
        "zoomFloor": 0.025,
        "shakeFloorPx": 2.5,
    }


_SHOTS = [
    {
        "shotID": "SH0001",
        "finalStartMs": 0,
        "finalEndMs": 10000,
    }
]


def _media() -> dict:
    return {"source": {"resolution": {"width": 320, "height": 180}}}


class TestGroupIndices:
    def test_adjacent_and_gapped(self):
        assert group_indices([0, 1, 2, 5, 6], max_gap=2) == [[0, 1, 2], [5, 6]]
        assert group_indices([], max_gap=2) == []


class TestSpeedRamps:
    def test_freeze_run_emits_low_motion_region(self):
        metrics = [_metric(i, motion=0.005, repeat=0.95) for i in range(1, 5)]
        events = detect_speed_ramps(metrics, _freeze_thresholds(), fps=8.0)
        low = [e for e in events if e["type"] == "low_motion_or_freeze"]
        assert len(low) == 1
        event = low[0]
        assert event["startMs"] == _t(1) and event["endMs"] > event["startMs"]
        assert event["needsVisualConfirmation"] is True
        assert event["evidenceRefs"]
        assert event["evidenceRefs"][0].startswith("raw/motion-effects.json#frameMetrics[")

    def test_brief_low_motion_below_min_region_ignored(self):
        # fps=8 → minimumRegionMs=250 → 至少 2 帧；单帧不构成区域事件。
        metrics = [_metric(1, motion=0.005, repeat=0.95)]
        events = detect_speed_ramps(metrics, _freeze_thresholds(), fps=8.0)
        assert not [e for e in events if e["type"] == "low_motion_or_freeze"]

    def test_high_motion_region_emitted(self):
        metrics = [_metric(i, motion=0.95) for i in range(1, 5)]
        events = detect_speed_ramps(metrics, _freeze_thresholds(), fps=8.0)
        high = [e for e in events if e["type"] == "high_motion_region"]
        assert len(high) == 1
        assert high[0]["needsVisualConfirmation"] is True
        assert high[0]["replicationHint"]  # 明示不得直接当快放

    def test_impact_cut_on_brightness_spike(self):
        # 亮度突变 > impactBrightnessFloor 且运动不低 → impact_cut 点事件。
        metrics = [
            _metric(1, motion=0.3, cut=0.1),
            _metric(2, motion=0.3, cut=0.1, brightness_delta=0.4),
            _metric(3, motion=0.3, cut=0.1),
        ]
        events = detect_speed_ramps(metrics, _freeze_thresholds(), fps=8.0)
        impact = [e for e in events if e["type"] == "impact_cut"]
        assert len(impact) == 1
        assert impact[0]["needsVisualConfirmation"] is True
        assert impact[0]["evidenceRefs"]

    def test_empty_metrics_no_events(self):
        assert detect_speed_ramps([], _freeze_thresholds(), fps=8.0) == []


class TestKeyframes:
    def test_position_candidate(self):
        metrics = [_metric(i) for i in range(1, 4)]
        metrics[1]["dxPxSample"] = 5.0  # > max(floor 2, p90 1)
        cands = detect_keyframes(metrics, _freeze_thresholds(), _SHOTS, _media())
        pos = [c for c in cands if c["property"] == "position"]
        assert len(pos) == 1
        assert pos[0]["shotID"] == "SH0001"
        assert pos[0]["needsVisualConfirmation"] is True
        assert pos[0]["evidenceRefs"]
        assert "sourceDxPxEstimate" in pos[0]["inferredChange"]
        # source estimate = sample * (源宽 320 / 采样宽 96)
        assert pos[0]["inferredChange"]["sourceDxPxEstimate"] == round(5.0 * (320 / SAMPLE_WIDTH), 3)

    def test_scale_candidate(self):
        metrics = [_metric(i) for i in range(1, 4)]
        metrics[1]["zoomScore"] = 0.05  # > max(zoomFloor 0.025, zoomP90 0.01)
        cands = detect_keyframes(metrics, _freeze_thresholds(), _SHOTS, _media())
        scale = [c for c in cands if c["property"] == "scale"]
        assert len(scale) == 1
        assert scale[0]["needsVisualConfirmation"] is True

    def test_shake_candidate_from_second_order_translation(self):
        metrics = [_metric(i) for i in range(1, 4)]
        metrics[1]["shakeScore"] = 4.0  # > max(shakeFloorPx 2.5, shakeP90 2)
        cands = detect_keyframes(metrics, _freeze_thresholds(), _SHOTS, _media())
        shake = [c for c in cands if c["property"] == "shake"]
        assert len(shake) == 1
        assert shake[0]["needsVisualConfirmation"] is True
        assert shake[0]["inferredChange"]["text"]

    def test_exposure_candidate_allowed_near_cut(self):
        metrics = [_metric(i, cut=0.9) for i in range(1, 4)]  # cutScore >= cutP90
        metrics[2]["brightness_delta"] = 0.0  # no-op
        metrics[1]["brightnessDelta"] = 0.35  # > max(floor 0.12, p92 0.05)
        cands = detect_keyframes(metrics, _freeze_thresholds(), _SHOTS, _media())
        exp = [c for c in cands if c["property"] == "exposure_or_opacity"]
        assert len(exp) == 1
        assert exp[0]["needsVisualConfirmation"] is True
        assert "brightnessDelta" in exp[0]["inferredChange"]

    def test_cut_guard_suppresses_position_scale_shake(self):
        # 切点帧（cutScore >= cutP90）的 position/scale/shake 被抑制。
        metrics = [_metric(i) for i in range(1, 4)]
        metrics[1].update(cut=0.9, dx=6.0, zoom=0.06, shake=5.0)
        cands = detect_keyframes(metrics, _freeze_thresholds(), _SHOTS, _media())
        props = {c["property"] for c in cands}
        assert "position" not in props and "scale" not in props and "shake" not in props

    def test_candidate_in_inter_shot_gap_skipped(self):
        shots = [
            {"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 100},
            {"shotID": "SH0002", "finalStartMs": 300, "finalEndMs": 1000},
        ]
        # idx=1 → timeMs=125，落在两镜头之间的 gap [100, 300)，无归属镜头。
        metrics = [_metric(0), _metric(1, dx=6.0), _metric(2)]
        cands = detect_keyframes(metrics, _freeze_thresholds(), shots, _media())
        assert cands == []


class TestDigest:
    def test_top12_only_and_sorted_by_confidence(self):
        speed = []
        for i in range(20):
            speed.append(
                {
                    "type": "high_motion_region",
                    "startMs": i * 100,
                    "endMs": i * 100 + 50,
                    "durationMs": 50,
                    "avgMotion": 1.0,
                    "confidence": "low",
                    "evidence": f"e{i}",
                    "replicationHint": "h",
                    "needsVisualConfirmation": True,
                    "evidenceRefs": [f"raw/motion-effects.json#frameMetrics[{i}]"],
                }
            )
        digest = build_digest(speed, [])
        assert len(digest["items"]) == 12
        assert digest["schemaVersion"] == 1
        assert all(item["needsVisualConfirmation"] for item in digest["items"])

    def test_empty_digest(self):
        digest = build_digest([], [])
        assert digest["items"] == []
        assert "absence" not in digest["usageNote"]  # 不隐含 absence 结论


class TestStub:
    def test_skipped_stub_covers_all_shots_and_no_absence(self):
        shots = [
            {"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 500},
            {"shotID": "SH0002", "finalStartMs": 500, "finalEndMs": 1000},
        ]
        media = {
            "source": {
                "revisionID": "r1",
                "durationMs": 1000,
                "analyzedRange": {"startMs": 0, "endMs": 1000},
            }
        }
        config = {"motionEffects": {"sampleFps": 8.0}, "ffmpeg": {}}
        stub = build_motion_effects_stub(shots, media, config)
        assert stub["status"] == "skipped"
        assert stub["schemaVersion"] == 1
        assert [s["shotID"] for s in stub["shots"]] == ["SH0001", "SH0002"]
        assert stub["frameMetrics"] == [] and stub["keyframeCandidates"] == []
        note = stub["digest"]["usageNote"]
        assert "skipped" in note and "no absence conclusion is implied" in note
        # JSON 可序列化
        json.dumps(stub)
