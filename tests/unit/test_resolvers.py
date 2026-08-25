"""analysis.resolvers 单元测试：确定性 resolver 与模型字段 resolver 的状态分派。

契约依据：docs/02 §3（五态）、docs/00 §4.2-4.4（absent 授权、追溯豁免）。
"""

from __future__ import annotations

import pytest

from memoloupe.analysis.observations import Confidence, Source, ValueState
from memoloupe.analysis.resolvers import (
    DEFAULT_RESOLVERS,
    AudioEnergyResolver,
    BgmPresenceResolver,
    CameraMovementResolver,
    ModelFieldResolver,
    QualityFlagsResolver,
    ShotEvidenceContext,
    SpeechResolver,
    build_observations,
    build_observations_with_review,
)

TWO_SHOTS = {
    "shots": [
        {"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 3203},
        {"shotID": "SH0002", "finalStartMs": 3203, "finalEndMs": 6400},
    ]
}


def _ctx(raws: dict[str, dict | None], shot_id: str = "SH0001") -> ShotEvidenceContext:
    return ShotEvidenceContext(shot_id=shot_id, raws=raws)


def _asr(status: str = "complete") -> dict:
    return {
        "status": status,
        "transcript": {
            "segments": [
                {"startMs": 820, "endMs": 2460, "text": "今天我们从机场出发。"},
                {"startMs": 3500, "endMs": 5200, "text": "第二段解说。"},
            ]
        },
    }


class TestSpeechResolver:
    def test_complete_joins_overlapping_segments(self):
        obs = SpeechResolver().resolve(_ctx({"shots": TWO_SHOTS, "asr": _asr()}))
        assert obs.field == "audio.speech"
        assert obs.state == ValueState.VALUE
        assert obs.value == "今天我们从机场出发。"
        assert obs.source == Source.ASR
        assert obs.evidence_refs == ("raw/asr.json#transcript.segments[0]",)

    def test_second_shot_uses_its_own_segment(self):
        obs = SpeechResolver().resolve(
            _ctx({"shots": TWO_SHOTS, "asr": _asr()}, "SH0002")
        )
        assert obs.state == ValueState.VALUE
        assert obs.value == "第二段解说。"
        assert obs.evidence_refs == ("raw/asr.json#transcript.segments[1]",)

    @pytest.mark.parametrize("status", ["skipped", "failed", "unavailable"])
    def test_non_complete_status_is_unknown(self, status: str):
        obs = SpeechResolver().resolve(
            _ctx({"shots": TWO_SHOTS, "asr": _asr(status)})
        )
        assert obs.state == ValueState.UNKNOWN

    def test_missing_file_is_unknown_without_refs(self):
        obs = SpeechResolver().resolve(_ctx({"shots": TWO_SHOTS, "asr": None}))
        assert obs.state == ValueState.UNKNOWN
        assert obs.evidence_refs == ()  # docs/00 §4.4 唯一豁免：能力未运行

    def test_no_overlapping_segment_is_unknown_not_absent(self):
        asr = _asr()
        asr["transcript"]["segments"] = [
            {"startMs": 9000, "endMs": 9500, "text": "远处的一句话。"}
        ]
        obs = SpeechResolver().resolve(_ctx({"shots": TWO_SHOTS, "asr": asr}))
        # ASR 不是授权确定性检测器，"没识别到语音" 只能是 unknown。
        assert obs.state == ValueState.UNKNOWN

    def test_boundary_segment_attribution_uses_overlap_ratio(self):
        """CALIBRATION：边界重叠句按交集比例 >= 0.5 归属（shot_speech 规则）。"""
        asr = _asr()
        # SH0001 [0,3203)：segment [3000,4000) 交集 203/1000 < 0.5 → 不归属
        asr["transcript"]["segments"] = [
            {"startMs": 3000, "endMs": 4000, "text": "跨界句。"}
        ]
        obs1 = SpeechResolver().resolve(_ctx({"shots": TWO_SHOTS, "asr": asr}))
        assert obs1.state == ValueState.UNKNOWN
        # SH0002 [3203,6400)：交集 797/1000 >= 0.5 → 归属
        obs2 = SpeechResolver().resolve(
            _ctx({"shots": TWO_SHOTS, "asr": asr}, "SH0002")
        )
        assert obs2.state == ValueState.VALUE
        assert obs2.value == "跨界句。"

    def test_asr_non_complete_falls_back_to_model_value(self):
        unified = _unified_media({"audio.speech": "模型听到的解说词"})
        obs = SpeechResolver().resolve(
            _ctx({"shots": TWO_SHOTS, "asr": _asr("skipped"), "unified-media": unified})
        )
        assert obs.state == ValueState.VALUE
        assert obs.value == "模型听到的解说词"
        assert obs.source == Source.UNIFIED_MODEL

    def test_asr_non_complete_model_absence_claim(self):
        unified = _unified_media({"audio.speech": "无"})
        obs = SpeechResolver().resolve(
            _ctx({"shots": TWO_SHOTS, "asr": _asr("failed"), "unified-media": unified})
        )
        assert obs.state == ValueState.ABSENT_CLAIMED
        assert obs.original_value == "无"
        assert obs.source == Source.UNIFIED_MODEL

    def test_asr_complete_wins_over_model(self):
        unified = _unified_media({"audio.speech": "模型解说"})
        obs = SpeechResolver().resolve(
            _ctx({"shots": TWO_SHOTS, "asr": _asr(), "unified-media": unified})
        )
        assert obs.state == ValueState.VALUE
        assert obs.value == "今天我们从机场出发。"
        assert obs.source == Source.ASR

    def test_asr_and_model_both_missing_is_unknown(self):
        obs = SpeechResolver().resolve(
            _ctx({"shots": TWO_SHOTS, "asr": None, "unified-media": None})
        )
        assert obs.state == ValueState.UNKNOWN
        assert obs.evidence_refs == ()


def _music_flags(state: str, status: str = "complete", confidence: str = "high") -> dict:
    return {
        "status": status,
        "shots": [{"shotID": "SH0001", "state": state, "confidence": confidence}],
    }


class TestBgmPresenceResolver:
    def test_music_state_is_value(self):
        obs = BgmPresenceResolver().resolve(
            _ctx({"music-flags": _music_flags("music")})
        )
        assert obs.state == ValueState.VALUE
        assert obs.value == "有"
        assert obs.confidence == Confidence.HIGH
        assert obs.evidence_refs == ("raw/music-flags.json#shots[0]",)

    def test_silent_state_is_deterministic_absent(self):
        obs = BgmPresenceResolver().resolve(
            _ctx({"music-flags": _music_flags("silent")})
        )
        assert obs.state == ValueState.ABSENT
        assert obs.value is None
        assert obs.source == Source.AUDIO_DETECTOR

    def test_unknown_state_stays_unknown_not_absent(self):
        obs = BgmPresenceResolver().resolve(
            _ctx({"music-flags": _music_flags("unknown", confidence="unknown")})
        )
        assert obs.state == ValueState.UNKNOWN
        assert obs.source != Source.AUDIO_DETECTOR or obs.state != ValueState.ABSENT

    def test_missing_file_is_unknown(self):
        obs = BgmPresenceResolver().resolve(_ctx({"music-flags": None}))
        assert obs.state == ValueState.UNKNOWN

    def test_non_complete_status_is_unknown(self):
        obs = BgmPresenceResolver().resolve(
            _ctx({"music-flags": _music_flags("music", status="failed")})
        )
        assert obs.state == ValueState.UNKNOWN


class TestAudioEnergyResolver:
    def test_shot_entry_maps_label_with_high_confidence(self):
        raw = {"hasAudio": True, "shots": [{"shotID": "SH0001", "label": "中"}]}
        obs = AudioEnergyResolver().resolve(_ctx({"audio-energy": raw}))
        assert obs.state == ValueState.VALUE
        assert obs.value == "中"
        assert obs.confidence == Confidence.HIGH
        assert obs.evidence_refs == ("raw/audio-energy.json#shots[0]",)

    def test_missing_shot_entry_is_unknown(self):
        raw = {"hasAudio": True, "shots": [{"shotID": "SH0002", "label": "低"}]}
        obs = AudioEnergyResolver().resolve(_ctx({"audio-energy": raw}))
        assert obs.state == ValueState.UNKNOWN

    def test_missing_file_is_unknown(self):
        obs = AudioEnergyResolver().resolve(_ctx({"audio-energy": None}))
        assert obs.state == ValueState.UNKNOWN


class TestQualityFlagsResolver:
    def _raw(self, confidence: str, flags: list[str], status: str = "complete") -> dict:
        return {
            "status": status,
            "shots": [
                {"shotID": "SH0001", "confidence": confidence, "flags": flags}
            ],
        }

    def test_flags_list_is_value(self):
        obs = QualityFlagsResolver().resolve(
            _ctx({"quality-flags": self._raw("high", ["画面模糊"])})
        )
        assert obs.state == ValueState.VALUE
        assert obs.value == ["画面模糊"]
        assert obs.confidence == Confidence.HIGH
        assert obs.source == Source.FFMPEG

    def test_empty_flags_list_is_legal_value(self):
        obs = QualityFlagsResolver().resolve(
            _ctx({"quality-flags": self._raw("medium", [])})
        )
        assert obs.state == ValueState.VALUE
        assert obs.value == []
        assert obs.confidence == Confidence.MEDIUM

    def test_unknown_confidence_is_unknown(self):
        obs = QualityFlagsResolver().resolve(
            _ctx({"quality-flags": self._raw("unknown", ["画面模糊"])})
        )
        assert obs.state == ValueState.UNKNOWN

    def test_non_complete_status_is_unknown(self):
        obs = QualityFlagsResolver().resolve(
            _ctx({"quality-flags": self._raw("high", [], status="partial")})
        )
        assert obs.state == ValueState.UNKNOWN


class TestCameraMovementResolver:
    def test_complete_with_value(self):
        raw = {
            "analysis": {"capabilityStatus": "complete"},
            "shots": [
                {"shotID": "SH0001", "cameraMovement": "pan_right", "confidence": "medium"}
            ],
        }
        obs = CameraMovementResolver().resolve(_ctx({"camera-motion": raw}))
        assert obs.state == ValueState.VALUE
        assert obs.value == "pan_right"
        assert obs.confidence == Confidence.MEDIUM
        assert obs.source == Source.APPLE_VISION
        assert obs.evidence_refs == ("raw/camera-motion.json#shots[0]",)

    def test_capability_not_complete_is_unknown(self):
        raw = {
            "analysis": {"capabilityStatus": "unavailable"},
            "shots": [
                {"shotID": "SH0001", "cameraMovement": "pan_right", "confidence": "medium"}
            ],
        }
        obs = CameraMovementResolver().resolve(_ctx({"camera-motion": raw}))
        assert obs.state == ValueState.UNKNOWN

    def test_missing_file_is_unknown(self):
        obs = CameraMovementResolver().resolve(_ctx({"camera-motion": None}))
        assert obs.state == ValueState.UNKNOWN


def _camera_motion(value: str, *, capability: str = "complete", confidence: str = "medium") -> dict:
    return {
        "analysis": {"capabilityStatus": capability},
        "shots": [
            {
                "shotID": "SH0001",
                "cameraMovement": value,
                "cameraMovementCandidates": [value],
                "confidence": confidence,
            }
        ],
    }


class TestCameraMovementResolverMerge:
    """D-005：Vision 与模型运镜并存；冲突进 review_reasons，不静默覆盖。"""

    def test_vision_wins_and_no_conflict_when_consistent(self):
        raws = {
            "camera-motion": _camera_motion("pan_right"),
            "unified-media": _unified_media({"visual.cameraMovement": "摇"}),
        }
        ctx = _ctx(raws)
        obs = CameraMovementResolver().resolve(ctx)
        assert obs.state == ValueState.VALUE
        assert obs.value == "pan_right"
        assert obs.source == Source.APPLE_VISION
        assert ctx.review_reasons == []

    def test_conflict_keeps_both_refs_and_collects_review_reason(self):
        raws = {
            "camera-motion": _camera_motion("pan_right"),
            "unified-media": _unified_media({"visual.cameraMovement": "推"}),
        }
        ctx = _ctx(raws)
        obs = CameraMovementResolver().resolve(ctx)
        # 主值仍是 Vision 分类，不被模型覆盖
        assert obs.state == ValueState.VALUE
        assert obs.value == "pan_right"
        assert obs.source == Source.APPLE_VISION
        # 双方证据都保留
        assert obs.evidence_refs == (
            "raw/camera-motion.json#shots[0]",
            "raw/unified-media.json#batches[0].response.shots[0].visual.cameraMovement",
        )
        assert len(ctx.review_reasons) == 1
        assert "pan_right" in ctx.review_reasons[0]

    def test_model_absence_claim_conflicts_with_detected_motion(self):
        raws = {
            "camera-motion": _camera_motion("zoom_in"),
            "unified-media": _unified_media({"visual.cameraMovement": "无"}),
        }
        ctx = _ctx(raws)
        obs = CameraMovementResolver().resolve(ctx)
        assert obs.value == "zoom_in"
        assert len(ctx.review_reasons) == 1

    def test_static_vision_and_model_absence_claim_is_not_conflict(self):
        raws = {
            "camera-motion": _camera_motion("static"),
            "unified-media": _unified_media({"visual.cameraMovement": "无"}),
        }
        ctx = _ctx(raws)
        obs = CameraMovementResolver().resolve(ctx)
        assert obs.value == "static"
        assert ctx.review_reasons == []

    def test_unmapped_model_value_does_not_conflict(self):
        raws = {
            "camera-motion": _camera_motion("pan_left"),
            "unified-media": _unified_media({"visual.cameraMovement": "螺旋环绕"}),
        }
        ctx = _ctx(raws)
        obs = CameraMovementResolver().resolve(ctx)
        assert obs.value == "pan_left"
        assert ctx.review_reasons == []  # unmapped 本身已可见，不重复报警

    def test_vision_unavailable_falls_back_to_model(self):
        raws = {
            "camera-motion": _camera_motion("pan_right", capability="unavailable"),
            "unified-media": _unified_media({"visual.cameraMovement": "摇"}),
        }
        obs = CameraMovementResolver().resolve(_ctx(raws))
        assert obs.state == ValueState.VALUE
        assert obs.value == "摇"  # 词表归一化
        assert obs.source == Source.UNIFIED_MODEL

    def test_vision_unknown_value_falls_back_to_model(self):
        raws = {
            "camera-motion": _camera_motion("unknown"),
            "unified-media": _unified_media({"visual.cameraMovement": "固定"}),
        }
        obs = CameraMovementResolver().resolve(_ctx(raws))
        assert obs.state == ValueState.VALUE
        assert obs.value == "固定"
        assert obs.source == Source.UNIFIED_MODEL

    def test_vision_unknown_value_without_model_keeps_vision_ref(self):
        raws = {"camera-motion": _camera_motion("unknown", confidence="unknown")}
        obs = CameraMovementResolver().resolve(_ctx(raws))
        assert obs.state == ValueState.UNKNOWN
        assert obs.evidence_refs == ("raw/camera-motion.json#shots[0]",)
        assert obs.source == Source.APPLE_VISION


def _unified_media(
    shot_fields: dict, *, shot_status: str = "succeeded", shot_id: str = "SH0001"
) -> dict:
    shot: dict = {"shotID": shot_id, "confidence": {"visual": "medium", "overall": "medium"}}
    for dotted, value in shot_fields.items():
        node = shot
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return {
        "status": "complete",
        "shotStatuses": {shot_id: shot_status},
        "batches": [
            {"shotIDs": [shot_id], "response": {"shots": [shot]}, "status": "complete"}
        ],
    }


class TestModelFieldResolver:
    def test_succeeded_shot_free_text_is_value(self):
        raw = _unified_media({"visual.content": "机场出发画面"})
        obs = ModelFieldResolver("visual.content").resolve(_ctx({"unified-media": raw}))
        assert obs.state == ValueState.VALUE
        assert obs.value == "机场出发画面"
        assert obs.source == Source.UNIFIED_MODEL
        assert obs.confidence == Confidence.MEDIUM
        assert obs.evidence_refs == (
            "raw/unified-media.json#batches[0].response.shots[0].visual.content",
        )

    def test_vocabulary_field_normalizes(self):
        raw = _unified_media({"visual.framing": "wide shot"})
        obs = ModelFieldResolver("visual.framing").resolve(_ctx({"unified-media": raw}))
        assert obs.state == ValueState.VALUE
        assert obs.value == "全景"

    def test_vocabulary_miss_is_unmapped_with_original(self):
        raw = _unified_media({"visual.framing": "无人机俯冲螺旋景"})
        obs = ModelFieldResolver("visual.framing").resolve(_ctx({"unified-media": raw}))
        assert obs.state == ValueState.UNMAPPED
        assert obs.original_value == "无人机俯冲螺旋景"

    def test_absence_claim_becomes_absent_claimed(self):
        raw = _unified_media({"components.compositingEvents": "无"})
        obs = ModelFieldResolver("components.compositingEvents").resolve(
            _ctx({"unified-media": raw})
        )
        assert obs.state == ValueState.ABSENT_CLAIMED
        assert obs.original_value == "无"
        assert obs.value is None

    def test_vocabulary_field_absence_claim_is_absent_claimed_not_unmapped(self):
        """词表字段的“无”同样是缺席声称，不得落 unmapped（docs/00 §4.2）。"""
        raw = _unified_media({"visual.framing": "无"})
        obs = ModelFieldResolver("visual.framing").resolve(_ctx({"unified-media": raw}))
        assert obs.state == ValueState.ABSENT_CLAIMED
        assert obs.original_value == "无"

    def test_model_unknown_text_is_unknown(self):
        raw = _unified_media({"visual.content": "unknown"})
        obs = ModelFieldResolver("visual.content").resolve(_ctx({"unified-media": raw}))
        assert obs.state == ValueState.UNKNOWN

    def test_pending_shot_is_unknown(self):
        raw = _unified_media({"visual.content": "机场"}, shot_status="pending")
        obs = ModelFieldResolver("visual.content").resolve(_ctx({"unified-media": raw}))
        assert obs.state == ValueState.UNKNOWN
        assert obs.evidence_refs == ()

    def test_missing_file_is_unknown(self):
        obs = ModelFieldResolver("visual.content").resolve(_ctx({"unified-media": None}))
        assert obs.state == ValueState.UNKNOWN

    def test_shape_mismatch_does_not_fall_back_to_first_element(self):
        """docs/04 §8.5 回归护栏：response 数组形状不符时按 shotID 查找，不得取 [0]。"""
        raw = _unified_media({"visual.content": "机场"}, shot_id="SH0002")
        obs = ModelFieldResolver("visual.content").resolve(
            _ctx({"unified-media": raw}, "SH0001")
        )
        assert obs.state == ValueState.UNKNOWN
        assert obs.value != "机场"


class TestBuildObservations:
    def test_one_observation_per_resolver_in_order(self):
        resolvers = [SpeechResolver(), BgmPresenceResolver()]
        observations = build_observations("SH0001", {}, resolvers)
        assert [o.field for o in observations] == ["audio.speech", "audio.bgmPresence"]
        assert all(o.shot_id == "SH0001" for o in observations)

    def test_default_resolvers_cover_deterministic_and_model_fields(self):
        fields = [r.field_name for r in DEFAULT_RESOLVERS]
        for expected in (
            "audio.speech",
            "audio.bgmPresence",
            "audio.energy",
            "quality.flags",
            "visual.cameraMovement",
            "visual.content",
            "visual.framing",
            "function.shotTone",
            "audio.bgmStyle",
            "editing.transition",
        ):
            assert expected in fields
        # 确定性来源优先：visual.cameraMovement 不得由模型 resolver 重复提供。
        assert fields.count("visual.cameraMovement") == 1
        assert len(fields) == len(set(fields))

    def test_build_observations_with_review_collects_reasons(self):
        raws = {
            "camera-motion": _camera_motion("pan_right"),
            "unified-media": _unified_media({"visual.cameraMovement": "推"}),
        }
        observations, review_reasons = build_observations_with_review(
            "SH0001", raws, [CameraMovementResolver()]
        )
        assert len(observations) == 1
        assert observations[0].value == "pan_right"
        assert len(review_reasons) == 1
        # 便捷包装保持原签名语义
        assert build_observations("SH0001", raws, [CameraMovementResolver()]) == observations
