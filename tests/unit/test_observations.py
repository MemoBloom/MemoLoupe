"""tests/unit/test_observations.py — Observation 五态状态机与构造守卫。"""

import pytest

from memoloupe.analysis.observations import (
    Confidence,
    Observation,
    Source,
    ValueState,
    apply_human_correction,
    deterministic_absent_observation,
    is_absence_claim,
    model_absent_observation,
    model_observation_from_raw,
    model_value_observation,
    unknown_observation,
    unmapped_observation,
)

EVIDENCE = ("raw/unified-media.json#batches[0].response.shots[0].visual.framing",)
FIELD = "visual.framing"
SHOT = "SH0001"


class TestEnums:
    def test_value_state_values(self):
        assert ValueState.VALUE == "value"
        assert ValueState.ABSENT == "absent"
        assert ValueState.ABSENT_CLAIMED == "absent-claimed"
        assert ValueState.UNKNOWN == "unknown"
        assert ValueState.UNMAPPED == "unmapped"

    def test_confidence_values(self):
        assert [c.value for c in Confidence] == ["high", "medium", "low", "unknown"]

    def test_source_values(self):
        assert Source.AUDIO_DETECTOR == "audioDetector"
        assert Source.APPLE_VISION == "appleVision"
        assert Source.UNIFIED_MODEL == "unifiedModel"
        assert Source.HUMAN == "human"
        assert Source.FALLBACK == "fallback"


class TestAllFiveStatesConstructible:
    def test_value_state(self):
        obs = model_value_observation(FIELD, SHOT, "全景", evidence_refs=EVIDENCE)
        assert obs.state == ValueState.VALUE
        assert obs.value == "全景"
        assert obs.verified is False

    def test_absent_state_only_from_deterministic(self):
        obs = deterministic_absent_observation(
            "components.texts", SHOT, evidence_refs=EVIDENCE, source=Source.FFMPEG
        )
        assert obs.state == ValueState.ABSENT
        assert obs.value is None

    def test_absent_claimed_state(self):
        obs = model_absent_observation("visual.props", SHOT, "无", evidence_refs=EVIDENCE)
        assert obs.state == ValueState.ABSENT_CLAIMED
        assert obs.value is None
        assert obs.original_value == "无"

    def test_unknown_state(self):
        obs = unknown_observation(FIELD, SHOT)
        assert obs.state == ValueState.UNKNOWN
        assert obs.value is None
        assert obs.evidence_refs == ()

    def test_unmapped_state(self):
        obs = unmapped_observation(FIELD, SHOT, "航拍", evidence_refs=EVIDENCE)
        assert obs.state == ValueState.UNMAPPED
        assert obs.original_value == "航拍"


class TestModelAbsentClaim:
    @pytest.mark.parametrize(
        "raw", ["无", "没有", "不存在", "none", "None", "NONE", "nothing", "n/a", " 无 "]
    )
    def test_absence_claims_recognized(self, raw):
        assert is_absence_claim(raw)

    @pytest.mark.parametrize("raw", ["无", "没有", "none"])
    def test_dispatch_produces_absent_claimed_never_absent(self, raw):
        obs = model_observation_from_raw("visual.props", SHOT, raw, evidence_refs=EVIDENCE)
        assert obs.state == ValueState.ABSENT_CLAIMED
        assert obs.state != ValueState.ABSENT
        assert obs.value is None
        assert obs.original_value == raw

    def test_dispatch_value(self):
        obs = model_observation_from_raw(FIELD, SHOT, "全景", evidence_refs=EVIDENCE)
        assert obs.state == ValueState.VALUE
        assert obs.value == "全景"

    @pytest.mark.parametrize("raw", [None, "", "unknown"])
    def test_dispatch_unknown(self, raw):
        obs = model_observation_from_raw(FIELD, SHOT, raw)
        assert obs.state == ValueState.UNKNOWN

    def test_deterministic_absent_rejects_model_source(self):
        with pytest.raises(ValueError):
            deterministic_absent_observation(
                "components.texts", SHOT, evidence_refs=EVIDENCE, source=Source.UNIFIED_MODEL
            )

    def test_model_absent_requires_raw_claim(self):
        with pytest.raises(ValueError):
            model_absent_observation("visual.props", SHOT, None, evidence_refs=EVIDENCE)


class TestAbsentClaimedNeverAutoPromoted:
    def test_human_correction_cannot_promote_model_claim_to_absent(self):
        obs = model_absent_observation("visual.props", SHOT, "无", evidence_refs=EVIDENCE)
        with pytest.raises(ValueError):
            apply_human_correction(obs, None, ValueState.ABSENT)

    def test_human_verify_unknown_cannot_become_absent(self):
        obs = unknown_observation(FIELD, SHOT, source=Source.UNIFIED_MODEL)
        with pytest.raises(ValueError):
            apply_human_correction(obs, None, ValueState.ABSENT)

    def test_human_can_confirm_deterministic_absent(self):
        obs = deterministic_absent_observation(
            "components.texts", SHOT, evidence_refs=EVIDENCE, source=Source.FFMPEG
        )
        corrected = apply_human_correction(obs, None, ValueState.ABSENT)
        assert corrected.state == ValueState.ABSENT
        assert corrected.source == Source.HUMAN
        assert corrected.verified is True


class TestVerifiedIndependentOfState:
    """docs/00 §4.3：verified 与五态相互独立，所有组合合法。"""

    def test_value_unverified(self):
        obs = model_value_observation(FIELD, SHOT, "全景", evidence_refs=EVIDENCE)
        assert obs.state == ValueState.VALUE and obs.verified is False

    def test_unknown_verified(self):
        obs = unknown_observation(FIELD, SHOT)
        corrected = apply_human_correction(obs, None, ValueState.UNKNOWN)
        assert corrected.state == ValueState.UNKNOWN and corrected.verified is True

    def test_absent_verified(self):
        obs = deterministic_absent_observation(
            "components.texts", SHOT, evidence_refs=EVIDENCE, source=Source.FFMPEG
        )
        corrected = apply_human_correction(obs, None, ValueState.ABSENT)
        assert corrected.verified is True

    def test_unmapped_unverified(self):
        obs = unmapped_observation(FIELD, SHOT, "航拍", evidence_refs=EVIDENCE)
        assert obs.state == ValueState.UNMAPPED and obs.verified is False


class TestHumanCorrection:
    def test_correction_sets_human_source_and_verified(self):
        obs = model_value_observation(FIELD, SHOT, "全景", evidence_refs=EVIDENCE)
        corrected = apply_human_correction(obs, "中景", ValueState.VALUE)
        assert corrected.source == Source.HUMAN
        assert corrected.verified is True
        assert corrected.value == "中景"
        assert corrected.original_value == "全景"

    def test_correction_keeps_evidence_by_default(self):
        obs = model_value_observation(FIELD, SHOT, "全景", evidence_refs=EVIDENCE)
        corrected = apply_human_correction(obs, "中景", ValueState.VALUE)
        assert corrected.evidence_refs == obs.evidence_refs

    def test_correction_from_unknown_to_value(self):
        obs = unknown_observation(FIELD, SHOT, source=Source.FALLBACK)
        corrected = apply_human_correction(obs, "近景", ValueState.VALUE, evidence_refs=EVIDENCE)
        assert corrected.state == ValueState.VALUE
        assert corrected.value == "近景"
        assert corrected.verified is True

    def test_original_observation_untouched(self):
        obs = model_value_observation(FIELD, SHOT, "全景", evidence_refs=EVIDENCE)
        apply_human_correction(obs, "中景", ValueState.VALUE)
        assert obs.value == "全景"
        assert obs.source == Source.UNIFIED_MODEL
        assert obs.verified is False


class TestUnmapped:
    def test_unmapped_requires_original_value(self):
        with pytest.raises(ValueError):
            unmapped_observation(FIELD, SHOT, None, evidence_refs=EVIDENCE)

    def test_unmapped_keeps_original_verbatim(self):
        raw = "  航拍镜头  "
        obs = unmapped_observation(FIELD, SHOT, raw, evidence_refs=EVIDENCE)
        assert obs.original_value == raw


class TestPostInitGuards:
    def test_value_state_requires_value(self):
        with pytest.raises(ValueError):
            Observation(
                field=FIELD,
                shot_id=SHOT,
                value=None,
                state=ValueState.VALUE,
                confidence=Confidence.MEDIUM,
                evidence_refs=EVIDENCE,
                source=Source.UNIFIED_MODEL,
            )

    def test_absent_state_forbids_value(self):
        with pytest.raises(ValueError):
            Observation(
                field=FIELD,
                shot_id=SHOT,
                value="全景",
                state=ValueState.ABSENT,
                confidence=Confidence.HIGH,
                evidence_refs=EVIDENCE,
                source=Source.FFMPEG,
            )

    def test_absent_claimed_forbids_value(self):
        with pytest.raises(ValueError):
            Observation(
                field=FIELD,
                shot_id=SHOT,
                value="x",
                state=ValueState.ABSENT_CLAIMED,
                confidence=Confidence.LOW,
                evidence_refs=EVIDENCE,
                source=Source.UNIFIED_MODEL,
                original_value="无",
            )

    def test_unmapped_requires_original_in_post_init(self):
        with pytest.raises(ValueError):
            Observation(
                field=FIELD,
                shot_id=SHOT,
                value=None,
                state=ValueState.UNMAPPED,
                confidence=Confidence.LOW,
                evidence_refs=EVIDENCE,
                source=Source.UNIFIED_MODEL,
            )

    def test_non_unknown_state_requires_evidence(self):
        with pytest.raises(ValueError):
            Observation(
                field=FIELD,
                shot_id=SHOT,
                value="全景",
                state=ValueState.VALUE,
                confidence=Confidence.MEDIUM,
                evidence_refs=(),
                source=Source.UNIFIED_MODEL,
            )

    def test_unknown_state_allows_empty_evidence(self):
        obs = Observation(
            field=FIELD,
            shot_id=SHOT,
            value=None,
            state=ValueState.UNKNOWN,
            confidence=Confidence.UNKNOWN,
            evidence_refs=(),
            source=Source.FALLBACK,
        )
        assert obs.state == ValueState.UNKNOWN

    def test_string_enums_coerced(self):
        obs = Observation(
            field=FIELD,
            shot_id=SHOT,
            value="全景",
            state="value",
            confidence="medium",
            evidence_refs=list(EVIDENCE),
            source="unifiedModel",
        )
        assert obs.state is ValueState.VALUE
        assert obs.confidence is Confidence.MEDIUM
        assert obs.source is Source.UNIFIED_MODEL
        assert obs.evidence_refs == EVIDENCE
