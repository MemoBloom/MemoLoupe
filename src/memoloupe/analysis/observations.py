"""analysis.observations — Observation 五态语义单元与构造守卫。

契约见 docs/02_DATA_AND_STATE_CONTRACTS.md §3 与 docs/00_REPRODUCTION_SPEC.md §4.2-4.4：

- ``absent`` 只能由授权确定性检测器产生；模型的“无/没有/none”一律降级为
  ``absent-claimed`` 并保留原文。
- ``verified`` 与 state 相互独立；人工修正不得把非确定性来源的观察改为 ``absent``。
- 非 ``unknown`` 状态必须携带 evidence_refs。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class ValueState(StrEnum):
    VALUE = "value"
    ABSENT = "absent"
    ABSENT_CLAIMED = "absent-claimed"
    UNKNOWN = "unknown"
    UNMAPPED = "unmapped"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class Source(StrEnum):
    FFPROBE = "ffprobe"
    FFMPEG = "ffmpeg"
    AUDIO_DETECTOR = "audioDetector"
    APPLE_VISION = "appleVision"
    ASR = "asr"
    UNIFIED_MODEL = "unifiedModel"
    TEXT_MODEL = "textModel"
    AGGREGATE = "aggregate"
    HUMAN = "human"
    FALLBACK = "fallback"


#: 授权产出 absent 的确定性检测器来源。
DETERMINISTIC_SOURCES: frozenset[Source] = frozenset(
    {Source.FFPROBE, Source.FFMPEG, Source.AUDIO_DETECTOR, Source.APPLE_VISION}
)

#: 模型输出中视为“声称不存在”的原文（strip 后、大小写不敏感匹配）。
ABSENCE_CLAIMS: frozenset[str] = frozenset(
    {"无", "没有", "不存在", "无内容", "none", "no", "nothing", "n/a", "null"}
)


def is_absence_claim(raw: object) -> bool:
    """判断模型原文是否为“无/没有”类声称。"""
    return isinstance(raw, str) and raw.strip().casefold() in ABSENCE_CLAIMS


@dataclass(frozen=True)
class Observation:
    """渲染层与人工校对层使用的统一语义单元。"""

    field: str
    shot_id: str
    value: object | None
    state: ValueState
    confidence: Confidence
    evidence_refs: tuple[str, ...]
    source: str
    verified: bool = False
    original_value: object | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", ValueState(self.state))
        object.__setattr__(self, "confidence", Confidence(self.confidence))
        object.__setattr__(self, "source", Source(self.source))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))

        if self.state == ValueState.VALUE and self.value is None:
            raise ValueError("state=value 时 value 不能为 None")
        if self.state in (
            ValueState.ABSENT,
            ValueState.ABSENT_CLAIMED,
            ValueState.UNKNOWN,
        ) and self.value is not None:
            raise ValueError(f"state={self.state} 时 value 必须为 None")
        if self.state == ValueState.UNMAPPED and self.original_value is None:
            raise ValueError("state=unmapped 时 original_value 必填")
        if self.state != ValueState.UNKNOWN and not self.evidence_refs:
            raise ValueError("非 unknown 状态必须携带至少一个 evidence_refs")


def model_value_observation(
    field: str,
    shot_id: str,
    value: object,
    *,
    confidence: Confidence | str = Confidence.UNKNOWN,
    evidence_refs: tuple[str, ...] = (),
    source: Source | str = Source.UNIFIED_MODEL,
) -> Observation:
    """模型给出具体值。"""
    if value is None:
        raise ValueError("model_value_observation 要求非 None 值")
    return Observation(
        field=field,
        shot_id=shot_id,
        value=value,
        state=ValueState.VALUE,
        confidence=confidence,
        evidence_refs=evidence_refs,
        source=source,
    )


def model_absent_observation(
    field: str,
    shot_id: str,
    raw_claim: object,
    *,
    confidence: Confidence | str = Confidence.UNKNOWN,
    evidence_refs: tuple[str, ...] = (),
    source: Source | str = Source.UNIFIED_MODEL,
) -> Observation:
    """模型声称“无/没有/不存在”——只能是 absent-claimed，绝不能产出 absent。"""
    if raw_claim is None:
        raise ValueError("absent-claimed 必须在 original_value 保留模型原文")
    return Observation(
        field=field,
        shot_id=shot_id,
        value=None,
        state=ValueState.ABSENT_CLAIMED,
        confidence=confidence,
        evidence_refs=evidence_refs,
        source=source,
        original_value=raw_claim,
    )


def deterministic_absent_observation(
    field: str,
    shot_id: str,
    *,
    confidence: Confidence | str = Confidence.HIGH,
    evidence_refs: tuple[str, ...] = (),
    source: Source | str,
) -> Observation:
    """仅授权确定性检测器可产出 absent。"""
    if Source(source) not in DETERMINISTIC_SOURCES:
        raise ValueError(f"absent 只能由授权确定性检测器产生，收到 source={source!r}")
    return Observation(
        field=field,
        shot_id=shot_id,
        value=None,
        state=ValueState.ABSENT,
        confidence=confidence,
        evidence_refs=evidence_refs,
        source=source,
    )


def unknown_observation(
    field: str,
    shot_id: str,
    *,
    confidence: Confidence | str = Confidence.UNKNOWN,
    evidence_refs: tuple[str, ...] = (),
    source: Source | str = Source.FALLBACK,
    original_value: object | None = None,
) -> Observation:
    """已运行但无法确认（或能力未运行）。"""
    return Observation(
        field=field,
        shot_id=shot_id,
        value=None,
        state=ValueState.UNKNOWN,
        confidence=confidence,
        evidence_refs=evidence_refs,
        source=source,
        original_value=original_value,
    )


def unmapped_observation(
    field: str,
    shot_id: str,
    original_value: object,
    *,
    confidence: Confidence | str = Confidence.UNKNOWN,
    evidence_refs: tuple[str, ...] = (),
    source: Source | str = Source.UNIFIED_MODEL,
) -> Observation:
    """模型有内容但词表无法映射；original_value 必填。"""
    if original_value is None:
        raise ValueError("unmapped 必须保留 original_value")
    return Observation(
        field=field,
        shot_id=shot_id,
        value=None,
        state=ValueState.UNMAPPED,
        confidence=confidence,
        evidence_refs=evidence_refs,
        source=source,
        original_value=original_value,
    )


def model_observation_from_raw(
    field: str,
    shot_id: str,
    raw: object,
    *,
    confidence: Confidence | str = Confidence.UNKNOWN,
    evidence_refs: tuple[str, ...] = (),
    source: Source | str = Source.UNIFIED_MODEL,
) -> Observation:
    """按模型原文分派：缺席声称 → absent-claimed；空/unknown → unknown；其余 → value。

    词表归一化由 Vocabulary 负责；命中失败时调用方应改用 unmapped_observation。
    """
    if raw is None or (isinstance(raw, str) and (not raw.strip() or raw.strip().casefold() == "unknown")):
        return unknown_observation(
            field, shot_id, confidence=confidence, evidence_refs=evidence_refs,
            source=source, original_value=raw,
        )
    if is_absence_claim(raw):
        return model_absent_observation(
            field, shot_id, raw, confidence=confidence, evidence_refs=evidence_refs, source=source
        )
    return model_value_observation(
        field, shot_id, raw, confidence=confidence, evidence_refs=evidence_refs, source=source
    )


def apply_human_correction(
    obs: Observation,
    new_value: object | None,
    new_state: ValueState | str,
    *,
    confidence: Confidence | str | None = None,
    evidence_refs: tuple[str, ...] | None = None,
) -> Observation:
    """应用人工修正：source=human、verified=True，original_value 记录修正前的 value。

    禁止把非确定性来源的观察仅经人工核实改为 absent——absent 只能来自授权
    检测器，或人工对确定性结果的确认。
    """
    new_state = ValueState(new_state)
    if new_state == ValueState.ABSENT and Source(obs.source) not in DETERMINISTIC_SOURCES:
        raise ValueError(
            f"不能仅经人工核实把 source={obs.source} 的观察改为 absent；"
            "absent 只能来自授权确定性检测器"
        )
    return replace(
        obs,
        value=new_value,
        state=new_state,
        confidence=obs.confidence if confidence is None else confidence,
        evidence_refs=obs.evidence_refs if evidence_refs is None else tuple(evidence_refs),
        source=Source.HUMAN,
        verified=True,
        original_value=obs.value,
    )
