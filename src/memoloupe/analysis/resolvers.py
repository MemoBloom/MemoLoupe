"""analysis.resolvers — 从 raw 证据构建 Observation 的字段 resolver。

契约见 docs/04 §1（渲染器先生成 Observation 再映射模板）、
docs/02 §3（五态状态机）与 docs/00 §4.2-4.4：

- 确定性检测优先于模型推断；``absent`` 只能由授权确定性检测器产生。
- 模型输出经 :func:`model_observation_from_raw` 分派，“无”只会得到
  ``absent-claimed``；受控词表字段经 :class:`Vocabulary` 归一化，
  无法映射时落 ``unmapped`` 并保留原文。
- raw 文件缺失或状态非 complete 时产 ``unknown``（能力未运行，
  evidence_refs 可为空 —— docs/00 §4.4 唯一豁免）。
- unified-media 的 response 数组按 shotID 查找，绝不按下标对齐
  （docs/04 §8.5 回归护栏）。
- ``audio.speech`` 优先级：ASR complete（shot_speech 交集归属）>
  unifiedModel 弱替代 > unknown（docs/03 §2.7）。
- ``visual.cameraMovement``（D-005）：Apple Vision 分类为主值，模型语义
  并存；矛盾时双 evidence_refs 保留并向 ``ShotEvidenceContext.review_reasons``
  收集 needsReview 理由，不静默覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Mapping, Protocol

from memoloupe.analysis.asr_stage import shot_speech_segments
from memoloupe.analysis.observations import (
    Confidence,
    Observation,
    Source,
    ValueState,
    deterministic_absent_observation,
    is_absence_claim,
    model_absent_observation,
    model_observation_from_raw,
    model_value_observation,
    unknown_observation,
    unmapped_observation,
)
from memoloupe.analysis.vocabulary import Vocabulary, load_vocabulary


@dataclass(frozen=True)
class ShotEvidenceContext:
    """单个镜头的全部 raw 证据视图。

    ``raws`` 键为逻辑名（``"asr"``、``"music-flags"``……），
    值为已解析的 raw 文件 dict；文件缺失或不可读时为 None。

    ``review_reasons`` 是 resolver 的收集槽（D-005）：确定性证据与模型
    语义冲突时追加人类可读理由，由 :func:`build_observations_with_review`
    返回给上层；resolver 不静默覆盖任何一方。
    """

    shot_id: str
    raws: Mapping[str, dict | None]
    review_reasons: list[str] = field(default_factory=list)


class FieldResolver(Protocol):
    """单个分析字段的 resolver 协议：每个 resolver 产出一个 Observation。"""

    field_name: str

    def resolve(self, context: ShotEvidenceContext) -> Observation: ...


@lru_cache(maxsize=1)
def _default_vocabulary() -> Vocabulary:
    return load_vocabulary()


def _to_confidence(value: object) -> Confidence:
    """把 raw 中的 confidence 字符串映射到 Confidence；非法值落 unknown。"""
    if isinstance(value, str):
        try:
            return Confidence(value)
        except ValueError:
            pass
    return Confidence.UNKNOWN


def _shot_entry(doc: dict, shot_id: str) -> tuple[int, dict] | None:
    """在 ``doc["shots"]`` 中按 shotID 查找，返回 (下标, 条目)；找不到返回 None。"""
    shots = doc.get("shots")
    if not isinstance(shots, list):
        return None
    for index, entry in enumerate(shots):
        if isinstance(entry, dict) and entry.get("shotID") == shot_id:
            return index, entry
    return None


def _status_complete(doc: dict | None) -> bool:
    return isinstance(doc, dict) and doc.get("status") == "complete"


def _not_run(field: str, shot_id: str) -> Observation:
    """能力未运行/未成功：unknown 且无 evidence_refs（docs/00 §4.4 豁免）。"""
    return unknown_observation(field, shot_id, source=Source.FALLBACK)


def _shot_final_range(context: ShotEvidenceContext) -> tuple[int, int] | None:
    shots_doc = context.raws.get("shots")
    if not shots_doc:
        return None
    found = _shot_entry(shots_doc, context.shot_id)
    if found is None:
        return None
    _, entry = found
    start, end = entry.get("finalStartMs"), entry.get("finalEndMs")
    if isinstance(start, int) and isinstance(end, int):
        return start, end
    return None


# ---------------------------------------------------------------------------
# 确定性 resolver
# ---------------------------------------------------------------------------


class SpeechResolver:
    """audio.speech：ASR > 模型的优先级解析（docs/01 §8、docs/03 §2.7）。

    - ASR complete：用 :func:`shot_speech_segments` 取与镜头 final 区间
      归属的 segments 拼文本（source=asr，refs 指具体 segment）。
      ASR 不在授权确定性检测器之列，“无归属 segment”只能是 unknown，
      绝不能落 absent。
    - ASR 非 complete：退回 unified-media 的 ``audio.speech`` 模型值
      （source=unifiedModel，置信度不升格；“无” → absent-claimed）。
    - 都没有：unknown（能力未运行，evidence_refs 豁免为空）。
    """

    field_name = "audio.speech"

    def resolve(self, context: ShotEvidenceContext) -> Observation:
        field, shot_id = self.field_name, context.shot_id
        doc = context.raws.get("asr")
        if not _status_complete(doc):
            model_obs = ModelFieldResolver(field).resolve(context)
            if model_obs.state != ValueState.UNKNOWN:
                return model_obs
            return _not_run(field, shot_id)
        assert doc is not None
        shot_range = _shot_final_range(context)
        if shot_range is None:
            return _not_run(field, shot_id)
        start, end = shot_range
        segments = doc.get("transcript", {}).get("segments")
        if not isinstance(segments, list):
            return _not_run(field, shot_id)
        hits = shot_speech_segments(segments, start, end)
        if not hits:
            return unknown_observation(
                field, shot_id, source=Source.ASR,
                evidence_refs=("raw/asr.json#transcript.segments",),
            )
        refs = tuple(f"raw/asr.json#transcript.segments[{i}]" for i, _ in hits)
        return model_value_observation(
            field,
            shot_id,
            " ".join(str(seg["text"]).strip() for _, seg in hits),
            confidence=Confidence.HIGH,
            evidence_refs=refs,
            source=Source.ASR,
        )


class BgmPresenceResolver:
    """audio.bgmPresence：music-flags.json 的逐镜头 music/silent 判定。

    ``silent`` 是确定性检测结果，可产 ``absent``（source=audioDetector）；
    其余状态一律 unknown，不得升格为 absent。
    """

    field_name = "audio.bgmPresence"

    def resolve(self, context: ShotEvidenceContext) -> Observation:
        field, shot_id = self.field_name, context.shot_id
        doc = context.raws.get("music-flags")
        if not _status_complete(doc):
            return _not_run(field, shot_id)
        assert doc is not None
        found = _shot_entry(doc, shot_id)
        if found is None:
            return _not_run(field, shot_id)
        index, entry = found
        ref = f"raw/music-flags.json#shots[{index}]"
        confidence = _to_confidence(entry.get("confidence"))
        state = entry.get("state")
        if state == "music":
            return model_value_observation(
                field, shot_id, "有",
                confidence=confidence, evidence_refs=(ref,),
                source=Source.AUDIO_DETECTOR,
            )
        if state == "silent":
            return deterministic_absent_observation(
                field, shot_id,
                confidence=confidence if confidence != Confidence.UNKNOWN else Confidence.HIGH,
                evidence_refs=(ref,),
                source=Source.AUDIO_DETECTOR,
            )
        return unknown_observation(
            field, shot_id, confidence=confidence,
            evidence_refs=(ref,), source=Source.AUDIO_DETECTOR,
            original_value=state,
        )


class AudioEnergyResolver:
    """audio.energy：audio-energy.json 有该镜头条目时取 label，confidence 固定 high。"""

    field_name = "audio.energy"

    def resolve(self, context: ShotEvidenceContext) -> Observation:
        field, shot_id = self.field_name, context.shot_id
        doc = context.raws.get("audio-energy")
        if not isinstance(doc, dict):
            return _not_run(field, shot_id)
        found = _shot_entry(doc, shot_id)
        if found is None:
            return _not_run(field, shot_id)
        index, entry = found
        label = entry.get("label")
        if not isinstance(label, str) or not label.strip():
            return _not_run(field, shot_id)
        return model_value_observation(
            field, shot_id, label.strip(),
            confidence=Confidence.HIGH,
            evidence_refs=(f"raw/audio-energy.json#shots[{index}]",),
            source=Source.FFMPEG,
        )


class QualityFlagsResolver:
    """quality.flags：quality-flags.json complete 且 confidence 非 unknown 时取 flags 列表。

    空列表是合法 value（确定性“无标记”），confidence=unknown 时整字段落 unknown。
    """

    field_name = "quality.flags"

    def resolve(self, context: ShotEvidenceContext) -> Observation:
        field, shot_id = self.field_name, context.shot_id
        doc = context.raws.get("quality-flags")
        if not _status_complete(doc):
            return _not_run(field, shot_id)
        assert doc is not None
        found = _shot_entry(doc, shot_id)
        if found is None:
            return _not_run(field, shot_id)
        index, entry = found
        confidence = _to_confidence(entry.get("confidence"))
        ref = f"raw/quality-flags.json#shots[{index}]"
        if confidence == Confidence.UNKNOWN:
            return unknown_observation(
                field, shot_id, confidence=confidence,
                evidence_refs=(ref,), source=Source.FFMPEG,
            )
        flags = entry.get("flags")
        if not isinstance(flags, list):
            return unknown_observation(
                field, shot_id, confidence=confidence,
                evidence_refs=(ref,), source=Source.FFMPEG,
                original_value=flags,
            )
        return model_value_observation(
            field, shot_id, [str(flag) for flag in flags],
            confidence=confidence, evidence_refs=(ref,),
            source=Source.FFMPEG,
        )


#: CALIBRATION（D-005）：Vision 运镜标签 → 可接受的模型规范值集合。
#: Vision 测图像运动、模型给摄影语义，两者认识论不同；只有明确的语义
#: 矛盾才触发 needsReview。未登记的标签（discontinuity/roll/unknown）
#: 不做比较——没有可靠映射时不臆造冲突。
_VISION_MODEL_COMPATIBLE: dict[str, frozenset[str]] = {
    "static": frozenset({"固定"}),
    "zoom_in": frozenset({"推"}),
    "zoom_out": frozenset({"拉"}),
    "pan_left": frozenset({"摇", "移"}),
    "pan_right": frozenset({"摇", "移"}),
    "tilt_up": frozenset({"摇", "升降"}),
    "tilt_down": frozenset({"摇", "升降"}),
    "handheld": frozenset({"手持"}),
}


def _camera_movement_conflict(vision_value: str, model_obs: Observation) -> bool:
    """判断模型语义标签是否与 Vision 分类矛盾（D-005）。

    只比较模型的 value（词表归一化后的规范值，可为 " → "/"、" 复合）与
    absent-claimed（"无"）；unmapped/unknown 不参与（本身已可见）。
    """
    compatible = _VISION_MODEL_COMPATIBLE.get(vision_value)
    if compatible is None:
        return False
    if model_obs.state == ValueState.ABSENT_CLAIMED:
        # 模型声称"无运镜"而 Vision 测到非 static 运动 → 矛盾。
        return vision_value != "static"
    if model_obs.state != ValueState.VALUE or not isinstance(model_obs.value, str):
        return False
    parts = [
        part.strip()
        for part in model_obs.value.replace("、", " → ").split(" → ")
        if part.strip()
    ]
    return bool(parts) and any(part not in compatible for part in parts)


class CameraMovementResolver:
    """visual.cameraMovement：Vision 运动证据优先，模型语义并存（D-005）。

    - camera-motion capabilityStatus=complete 且该镜头有具体分类 → 主值取
      Vision 分类（source=appleVision）；若模型也有该字段值且语义矛盾，
      把双方 evidence_refs 都保留并向 context.review_reasons 追加
      needsReview 理由，不静默覆盖任一方。
    - Vision 不可用/该镜头无值 → 退回模型值（source=unifiedModel）。
    - 都没有 → unknown。
    """

    field_name = "visual.cameraMovement"

    def resolve(self, context: ShotEvidenceContext) -> Observation:
        field, shot_id = self.field_name, context.shot_id
        doc = context.raws.get("camera-motion")
        vision_value: str | None = None
        vision_ref: str | None = None
        vision_confidence = Confidence.UNKNOWN
        vision_ran = False
        if isinstance(doc, dict):
            analysis = doc.get("analysis")
            vision_ran = (
                isinstance(analysis, dict)
                and analysis.get("capabilityStatus") == "complete"
            )
            if vision_ran:
                found = _shot_entry(doc, shot_id)
                if found is not None:
                    index, entry = found
                    vision_ref = f"raw/camera-motion.json#shots[{index}]"
                    vision_confidence = _to_confidence(entry.get("confidence"))
                    raw_value = entry.get("cameraMovement")
                    if (
                        isinstance(raw_value, str)
                        and raw_value.strip()
                        and raw_value.strip() != "unknown"
                    ):
                        vision_value = raw_value.strip()

        model_obs = ModelFieldResolver(field).resolve(context)

        if vision_value is not None:
            refs = [vision_ref] if vision_ref else []
            if _camera_movement_conflict(vision_value, model_obs):
                refs.extend(model_obs.evidence_refs)
                context.review_reasons.append(
                    f"{field}：Apple Vision={vision_value} 与模型语义"
                    f"（{model_obs.original_value or model_obs.value}）不一致，需人工复核"
                )
            return model_value_observation(
                field, shot_id, vision_value,
                confidence=vision_confidence, evidence_refs=tuple(refs),
                source=Source.APPLE_VISION,
            )
        if model_obs.state != ValueState.UNKNOWN:
            return model_obs
        if vision_ran and vision_ref is not None:
            # Vision 跑了但该镜头信号不足（unknown）：保留证据引用。
            return unknown_observation(
                field, shot_id, confidence=vision_confidence,
                evidence_refs=(vision_ref,), source=Source.APPLE_VISION,
            )
        return _not_run(field, shot_id)


# ---------------------------------------------------------------------------
# 模型字段 resolver
# ---------------------------------------------------------------------------

#: 字段前缀 -> unified-media 逐镜头 confidence 子键。
_CONFIDENCE_SECTIONS: dict[str, str] = {
    "visual": "visual",
    "audio": "audio",
    "editing": "editing",
}


class ModelFieldResolver:
    """模型字段：unified-media.json 该 shot 成功时取 response 中的值。

    - 词表登记字段经 Vocabulary 归一化：命中 → value（规范值），
      未命中 → unmapped（保留原文）；
    - 自由文本字段经 model_observation_from_raw 分派：
      “无/没有” → absent-claimed（绝不升格 absent），空/unknown → unknown；
    - shotStatuses 非 succeeded、文件缺失或 response 形状不符 → unknown。
    """

    def __init__(self, field_name: str, path: str | None = None) -> None:
        self.field_name = field_name
        self.path = path if path is not None else field_name

    def resolve(self, context: ShotEvidenceContext) -> Observation:
        field, shot_id = self.field_name, context.shot_id
        doc = context.raws.get("unified-media")
        if not isinstance(doc, dict):
            return _not_run(field, shot_id)
        statuses = doc.get("shotStatuses")
        if not isinstance(statuses, dict) or statuses.get(shot_id) != "succeeded":
            return _not_run(field, shot_id)
        located = self._locate_shot(doc, shot_id)
        if located is None:
            return _not_run(field, shot_id)
        batch_index, shot_index, shot = located
        base_ref = f"raw/unified-media.json#batches[{batch_index}].response.shots[{shot_index}]"
        raw_value: object = shot
        for part in self.path.split("."):
            if not isinstance(raw_value, dict) or part not in raw_value:
                return unknown_observation(
                    field, shot_id, evidence_refs=(base_ref,),
                    source=Source.UNIFIED_MODEL,
                )
            raw_value = raw_value[part]
        ref = f"{base_ref}.{self.path}"
        confidence = self._confidence(shot)
        # “无/没有”类声称优先于词表归一化：任何模型字段都只能 absent-claimed，
        # 不得因词表未命中而落 unmapped（docs/00 §4.2-4.3）。
        if is_absence_claim(raw_value):
            return model_absent_observation(
                field, shot_id, raw_value,
                confidence=confidence, evidence_refs=(ref,),
                source=Source.UNIFIED_MODEL,
            )
        vocabulary = _default_vocabulary()
        if field in vocabulary.fields:
            result = vocabulary.normalize(field, raw_value)
            if result.status == "value":
                assert result.value is not None
                return model_value_observation(
                    field, shot_id, result.value,
                    confidence=confidence, evidence_refs=(ref,),
                    source=Source.UNIFIED_MODEL,
                )
            if result.status == "unmapped":
                return unmapped_observation(
                    field, shot_id, result.original,
                    confidence=confidence, evidence_refs=(ref,),
                    source=Source.UNIFIED_MODEL,
                )
            return unknown_observation(
                field, shot_id, confidence=confidence, evidence_refs=(ref,),
                source=Source.UNIFIED_MODEL, original_value=result.original,
            )
        return model_observation_from_raw(
            field, shot_id, raw_value,
            confidence=confidence, evidence_refs=(ref,),
            source=Source.UNIFIED_MODEL,
        )

    @staticmethod
    def _locate_shot(doc: dict, shot_id: str) -> tuple[int, int, dict] | None:
        """按 shotID 在 batches[].response.shots[] 中查找；绝不按下标对齐。"""
        batches = doc.get("batches")
        if not isinstance(batches, list):
            return None
        for batch_index, batch in enumerate(batches):
            if not isinstance(batch, dict):
                continue
            response = batch.get("response")
            if not isinstance(response, dict):
                continue
            shots = response.get("shots")
            if not isinstance(shots, list):
                continue
            for shot_index, shot in enumerate(shots):
                if isinstance(shot, dict) and shot.get("shotID") == shot_id:
                    return batch_index, shot_index, shot
        return None

    def _confidence(self, shot: dict) -> Confidence:
        confidence = shot.get("confidence")
        if not isinstance(confidence, dict):
            return Confidence.UNKNOWN
        section = _CONFIDENCE_SECTIONS.get(self.field_name.split(".", 1)[0], "overall")
        return _to_confidence(confidence.get(section) or confidence.get("overall"))


#: 本阶段模型字段清单（deterministic resolver 已覆盖的字段不在此列）。
MODEL_FIELDS: tuple[str, ...] = (
    "visual.content",
    "visual.subjects",
    "visual.actions",
    "visual.setting",
    "visual.props",
    "visual.framing",
    "visual.subjectCoverage",
    "visual.cameraAngle",
    "visual.composition",
    "visual.perspective",
    "visual.brightness",
    "visual.contrast",
    "visual.lightingType",
    "visual.colorTemperature",
    "visual.saturation",
    "visual.depthOfField",
    "visual.texture",
    "visual.dominantColor",
    "visual.lensFeel",
    "visual.movementIntensity",
    "function.sourceMedium",
    "function.subjectEmotion",
    "function.shotTone",
    "audio.bgmStyle",
    "audio.soundEffects",
    "components.compositingEvents",
    "editing.transition",
    "editing.continuity",
)

#: 默认 resolver 集合：确定性字段优先，其次模型字段。
DEFAULT_RESOLVERS: tuple[FieldResolver, ...] = (
    SpeechResolver(),
    BgmPresenceResolver(),
    AudioEnergyResolver(),
    QualityFlagsResolver(),
    CameraMovementResolver(),
    *(ModelFieldResolver(field) for field in MODEL_FIELDS),
)


def build_observations_with_review(
    shot_id: str,
    raws: Mapping[str, dict | None],
    resolvers: tuple[FieldResolver, ...] | list[FieldResolver],
) -> tuple[list[Observation], tuple[str, ...]]:
    """产 Observations 并收集 resolver 报告的 needsReview 理由（D-005）。"""
    context = ShotEvidenceContext(shot_id=shot_id, raws=raws)
    observations = [resolver.resolve(context) for resolver in resolvers]
    return observations, tuple(context.review_reasons)


def build_observations(
    shot_id: str,
    raws: Mapping[str, dict | None],
    resolvers: tuple[FieldResolver, ...] | list[FieldResolver],
) -> list[Observation]:
    """对每个 resolver 产一个 Observation；顺序与 resolvers 一致。"""
    observations, _ = build_observations_with_review(shot_id, raws, resolvers)
    return observations
