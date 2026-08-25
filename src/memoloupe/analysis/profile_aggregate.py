"""analysis.profile_aggregate — Phase 3 确定性聚合（docs/03 §4.1、roadmap 04-01）。

纯函数友好模块，**不导入或调用任何模型服务**。从稳定 JSON（media/shots/asr/
music-flags/audio-cuts/camera-motion/unified-media/story-blocks）计算
``style-profile.json`` 的全部确定性字段：

- structure：slot 序列（L1 types/durationShare/rangeSeconds/minBlocks、L3
  shotIds/shotCount/avgShotSeconds）、expectationChains（由 blockRelation
  的跨 slot 引用确定性提取）；turns/nonLinearDevices 保守空数组；
  hook/payoff 为 null（主观定位属模型层）；
- pacing：shotDuration（mean/p50，镜头 >= 5 时附 p10/p90）、densityCurve
  （slot 内 blocks 的最常见非 unknown narrativeDensity，全 unknown 落
  "unknown"）、slotPacing、audioBoundaryBySlot（同步切边界占可判定边界
  比例 >= AUDIO_ALIGN_THRESHOLD）、musicAlignment（确定性信号，见
  :func:`_music_alignment`）；
- style：transitions/framing/lighting 分布（unified 模型值经受控词表归一化，
  unknown/unmapped 不入分布）、cameraMovement 分布（camera-motion.json 的
  Apple Vision 确定性值，不映射为摄影术语，docs/02 §4.8）、textOverlay/
  bgm/voiceMix coverage（时间并集计权）、hostedCoverage（明确人物词才计入，
  否则 0.0——保守可解释值）；
- asrTextStats：segmentCount/characterCount/speechDurationMs。

模型主观字段输出合法占位：functionalTitle/narrativeFunction/intendedReaction
与 L2 为 null/空串、structureRequirements/discussionItems 为空数组、
adoptionHints 为 null；``distillStatus="skipped"``（未请求蒸馏）。

数值规则（docs/03 §4.1）：分布默认按镜头数计权；coverage 按时间并集占
分析范围比例；内部计算保留精度，仅在最终文档序列化时舍入（比例 4 位、
时长 3 位）。
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping

import numpy as np

from memoloupe.analysis.vocabulary import Vocabulary, load_vocabulary

PROFILE_AGGREGATE_VERSION = "profile-aggregate.v1"
SCHEMA_VERSION = 2

#: audioBoundaryBySlot：同步切边界占可判定边界比例达到该阈值才算 aligned。
#: CALIBRATION（docs/06 A-007）。
AUDIO_ALIGN_THRESHOLD = 0.5

#: musicAlignment：块内部边界被 BGM 区间覆盖的比例阈值。CALIBRATION A-007。
MUSIC_ALIGN_THRESHOLD = 0.5

#: hostedCoverage 的人物出镜关键词（subjects 命中任一词才计入覆盖）。
#: CALIBRATION A-007——无法可靠判断时采用保守 0.0，不编造。
HOSTED_KEYWORDS: tuple[str, ...] = (
    "人物", "主持人", "主播", "讲解", "受访", "嘉宾",
    "旅客", "游客", "旅行者", "人群", "路人", "出镜", "采访", "主角",
)

#: style 分布字段：unified 字段路径 -> 受控词表字段名。
_DISTRIBUTION_FIELDS: dict[str, tuple[str, str]] = {
    "transitions": ("editing", "transition", "editing.transition"),
    "framing": ("visual", "framing", "visual.framing"),
    "lighting": ("visual", "lightingType", "visual.lightingType"),
}

_BLOCK_RELATION_REF_RE = re.compile(r"^\s*(.+?)\s*→\s*(B\d{4})\s*$")

def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _seconds(ms: int) -> float:
    return round(ms / 1000.0, 3)


def _ratio(value: float) -> float:
    return round(value, 4)


# ---------------------------------------------------------------------------
# 镜头与故事索引
# ---------------------------------------------------------------------------


def shots_sorted(shots_doc: Mapping | None) -> list[dict]:
    """shots.json 的镜头按 finalStartMs 升序；非法条目跳过。"""
    if not shots_doc:
        return []
    entries = shots_doc.get("shots")
    if not isinstance(entries, list):
        return []
    shots = [
        s for s in entries
        if isinstance(s, dict) and isinstance(s.get("shotID"), str)
        and isinstance(s.get("finalStartMs"), int)
        and isinstance(s.get("finalEndMs"), int)
    ]
    return sorted(shots, key=lambda s: s["finalStartMs"])


def _shots_by_id(shots: list[dict]) -> dict[str, dict]:
    return {str(s["shotID"]): s for s in shots}


def _analyzed_range_ms(media: Mapping | None) -> tuple[int, int] | None:
    """分析范围 [startMs, endMs)；不可用时 None。"""
    if not media:
        return None
    arange = media.get("source", {}).get("analyzedRange")
    if not isinstance(arange, dict):
        return None
    start = _as_int(arange.get("startMs"))
    end = _as_int(arange.get("endMs"))
    if start is None or end is None or end <= start:
        return None
    return start, end


def _total_duration_ms(shots: list[dict]) -> int:
    return sum(_as_int(s.get("durationMs")) or 0 for s in shots)


def _blocks_by_id(story: Mapping | None) -> dict[str, dict]:
    if not story:
        return {}
    blocks = story.get("blocks")
    if not isinstance(blocks, list):
        return {}
    return {
        str(b["storyBlockID"]): b
        for b in blocks
        if isinstance(b, dict) and isinstance(b.get("storyBlockID"), str)
    }


def _slot_blocks(slot: Mapping, blocks_by_id: dict[str, dict]) -> list[dict]:
    """slot 的 block 按 startMs 升序；引用未知 block 跳过。"""
    ordered: list[dict] = []
    for bid in slot.get("blockIDs", []):
        block = blocks_by_id.get(str(bid))
        if block is not None:
            ordered.append(block)
    return sorted(ordered, key=lambda b: (b.get("startMs") is None, b.get("startMs", 0)))


def _slot_shot_ids(slot_blocks: list[dict]) -> list[str]:
    ids: list[str] = []
    for block in slot_blocks:
        for sid in block.get("shotIDs", []):
            if isinstance(sid, str) and sid not in ids:
                ids.append(sid)
    return ids


def _slot_duration_ms(slot_blocks: list[dict]) -> int:
    if not slot_blocks:
        return 0
    return int(slot_blocks[-1]["endMs"]) - int(slot_blocks[0]["startMs"])


# ---------------------------------------------------------------------------
# structure.slots
# ---------------------------------------------------------------------------


def _slot_types(slot: Mapping) -> list[str]:
    """slotType 按顿号切分为 types 数组；空值输出空数组（不伪造）。"""
    raw = slot.get("slotType")
    if not isinstance(raw, str) or not raw.strip():
        return []
    return [part.strip() for part in raw.split("、") if part.strip()]


def _profile_slot(
    slot: Mapping,
    slot_blocks: list[dict],
    shots_by_id: dict[str, dict],
    total_ms: int,
) -> dict:
    shot_ids = _slot_shot_ids(slot_blocks)
    durations = [
        _as_int(shots_by_id[sid].get("durationMs"))
        for sid in shot_ids
        if sid in shots_by_id
    ]
    durations = [d for d in durations if d is not None]
    avg = sum(durations) / len(durations) / 1000.0 if durations else 0.0
    slot_ms = _slot_duration_ms(slot_blocks)
    return {
        "slotId": str(slot["slotID"]),
        "L1": {
            "types": _slot_types(slot),
            "functionalTitle": None,
            "narrativeFunction": None,
            "durationShare": _ratio(slot_ms / total_ms) if total_ms else 0.0,
            "rangeSeconds": [
                _seconds(slot_blocks[0]["startMs"]),
                _seconds(slot_blocks[-1]["endMs"]),
            ],
            "intendedReaction": None,
            "minBlocks": len(slot_blocks),
        },
        "L2": {
            "carriage": None,
            "pattern": None,
            "referenceContent": "",
        },
        "L3": {
            "shotIds": shot_ids,
            "shotCount": len(shot_ids),
            "avgShotSeconds": round(avg, 3),
        },
    }


# ---------------------------------------------------------------------------
# structure：期望链 / 转折 / 非线性设备
# ---------------------------------------------------------------------------


def _expectation_chains(
    story: Mapping, blocks_by_id: dict[str, dict]
) -> list[dict]:
    """从 block.blockRelation 确定性提取跨 slot 期望链。

    匹配 ``<kind> → Bxxxx``；目标块存在且与源块分属不同 slot 才计入
    （同一 slot 内引用不是跨阶段期望链）。
    """
    block_to_slot: dict[str, str] = {}
    slots = story.get("slots") if isinstance(story, dict) else None
    if isinstance(slots, list):
        for slot in slots:
            if not isinstance(slot, dict) or not isinstance(slot.get("slotID"), str):
                continue
            for bid in slot.get("blockIDs", []):
                if isinstance(bid, str) and bid not in block_to_slot:
                    block_to_slot[bid] = slot["slotID"]
    chains: list[dict] = []
    for block in blocks_by_id.values():
        relation = block.get("blockRelation")
        if not isinstance(relation, str):
            continue
        match = _BLOCK_RELATION_REF_RE.match(relation)
        if match is None:
            continue
        kind, target = match.group(1).strip(), match.group(2)
        from_slot = block_to_slot.get(str(block["storyBlockID"]))
        to_slot = block_to_slot.get(target)
        if from_slot is None or to_slot is None or from_slot == to_slot:
            continue
        chains.append(
            {
                "kind": kind,
                "fromSlot": from_slot,
                "toSlot": to_slot,
                "evidence": {
                    "blockId": str(block["storyBlockID"]),
                    "relation": relation,
                },
            }
        )
    return chains


def _structure(
    story: Mapping,
    blocks_by_id: dict[str, dict],
    shots_by_id: dict[str, dict],
    total_ms: int,
) -> dict:
    slots_out: list[dict] = []
    slots = story.get("slots") if isinstance(story, dict) else None
    if isinstance(slots, list):
        for slot in slots:
            if not isinstance(slot, dict) or not isinstance(slot.get("slotID"), str):
                continue
            slots_out.append(
                _profile_slot(slot, _slot_blocks(slot, blocks_by_id), shots_by_id, total_ms)
            )
    return {
        "slots": slots_out,
        "hook": None,
        "payoff": None,
        "turns": [],
        "nonLinearDevices": [],
        "expectationChains": _expectation_chains(story, blocks_by_id),
    }


# ---------------------------------------------------------------------------
# pacing
# ---------------------------------------------------------------------------


def _shot_duration_stats(shots: list[dict]) -> dict:
    """全片镜头时长 mean/p50（秒）；镜头 >= 5 时附 p10/p90。"""
    durations = sorted(
        d for s in shots
        if (_as_int(s.get("durationMs"))) is not None
        for d in [s["durationMs"] / 1000.0]
    )
    if not durations:
        return {}
    stats: dict[str, float] = {
        "mean": round(float(np.mean(durations)), 3),
        "p50": round(float(np.percentile(durations, 50)), 3),
    }
    if len(durations) >= 5:
        stats["p10"] = round(float(np.percentile(durations, 10)), 3)
        stats["p90"] = round(float(np.percentile(durations, 90)), 3)
    return stats


def _slot_density(slot_blocks: list[dict]) -> str:
    """slot 内 blocks 最常见的非 unknown narrativeDensity；全 unknown 落 unknown。"""
    counts: Counter[str] = Counter()
    for block in slot_blocks:
        density = block.get("narrativeDensity")
        if isinstance(density, str) and density.strip() and density != "unknown":
            counts[density.strip()] += 1
    if not counts:
        return "unknown"
    return counts.most_common(1)[0][0]


def _audio_boundary_by_slot(
    slots: list[dict],
    blocks_by_id: dict[str, dict],
    audio_cuts: Mapping | None,
) -> list[dict]:
    """每 slot：内部块边界中 synchronizedCut 占可判定边界的比例 >= 阈值。

    接缝取前块末镜头 boundaryOut 的 classification（audio-cuts.json）；
    无内部边界视为真空对齐 true；有内部边界但全部不可判定视为 false
    （不能证明对齐）。
    """
    by_shot: dict[str, dict] = {}
    if audio_cuts:
        for entry in audio_cuts.get("shots", []):
            if isinstance(entry, dict) and isinstance(entry.get("shotID"), str):
                by_shot[entry["shotID"]] = entry
    result: list[dict] = []
    for slot in slots:
        slot_blocks = _slot_blocks(slot, blocks_by_id)
        seams: list[str] = []
        for i in range(len(slot_blocks) - 1):
            prev_block = slot_blocks[i]
            shot_ids = prev_block.get("shotIDs", [])
            if shot_ids and isinstance(shot_ids[-1], str):
                seams.append(shot_ids[-1])
        if not seams:
            result.append({"slotId": str(slot["slotID"]), "boundaryAligned": True})
            continue
        classifications: list[str] = []
        for shot_id in seams:
            entry = by_shot.get(shot_id)
            boundary_out = entry.get("boundaryOut") if isinstance(entry, dict) else None
            classification = (
                boundary_out.get("classification")
                if isinstance(boundary_out, dict)
                else None
            )
            if isinstance(classification, str) and classification != "unavailable":
                classifications.append(classification)
        if not classifications:
            result.append({"slotId": str(slot["slotID"]), "boundaryAligned": False})
            continue
        aligned = (
            sum(1 for c in classifications if c == "synchronizedCut")
            / len(classifications)
        )
        result.append(
            {
                "slotId": str(slot["slotID"]),
                "boundaryAligned": aligned >= AUDIO_ALIGN_THRESHOLD,
            }
        )
    return result


def _music_alignment(
    blocks: list[dict], music: Mapping | None
) -> str:
    """BGM 区间与故事块内部边界对齐的确定性信号。CALIBRATION A-007。

    - music 检测不可用/无区间 → ``no music detected``；
    - 无内部块边界 → ``unknown``（无接缝可对齐）；
    - 覆盖边界比例 >= 阈值 → ``music aligned with story boundaries``；
    - 否则 → ``music not aligned with story boundaries``。
    """
    if not music or music.get("status") != "complete":
        return "no music detected"
    intervals = music.get("musicIntervals")
    if not isinstance(intervals, list) or not intervals:
        return "no music detected"
    seams = [b.get("endMs") for b in blocks[:-1]]
    if not seams:
        return "unknown"

    def covers(ms: int) -> bool:
        for interval in intervals:
            if not isinstance(interval, dict):
                continue
            start = interval.get("startSec")
            end = interval.get("endSec")
            if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                if start * 1000 <= ms < end * 1000:
                    return True
        return False

    ratio = sum(1 for s in seams if covers(s)) / len(seams)
    if ratio >= MUSIC_ALIGN_THRESHOLD:
        return "music aligned with story boundaries"
    return "music not aligned with story boundaries"


def _pacing(
    slots: list[dict],
    blocks_by_id: dict[str, dict],
    shots_by_id: dict[str, dict],
    shots: list[dict],
    audio_cuts: Mapping | None,
    music: Mapping | None,
    blocks: list[dict],
) -> dict:
    density_curve: list[dict] = []
    slot_pacing: list[dict] = []
    for slot in slots:
        slot_blocks = _slot_blocks(slot, blocks_by_id)
        density_curve.append(
            {"slotId": str(slot["slotID"]), "density": _slot_density(slot_blocks)}
        )
        shot_ids = _slot_shot_ids(slot_blocks)
        durations = [
            _as_int(shots_by_id[sid].get("durationMs"))
            for sid in shot_ids
            if sid in shots_by_id
        ]
        durations = [d for d in durations if d is not None]
        slot_pacing.append(
            {
                "slotId": str(slot["slotID"]),
                "shotCount": len(shot_ids),
                "avgShotSeconds": round(
                    sum(durations) / len(durations) / 1000.0 if durations else 0.0, 3
                ),
            }
        )
    return {
        "shotDuration": _shot_duration_stats(shots),
        "densityCurve": density_curve,
        "slotPacing": slot_pacing,
        "audioBoundaryBySlot": _audio_boundary_by_slot(slots, blocks_by_id, audio_cuts),
        "musicAlignment": _music_alignment(blocks, music),
    }


# ---------------------------------------------------------------------------
# style 分布与 coverage
# ---------------------------------------------------------------------------


def _model_shots_by_id(unified: Mapping | None) -> dict[str, dict]:
    if not unified:
        return {}
    result: dict[str, dict] = {}
    for batch in unified.get("batches", []):
        if not isinstance(batch, dict):
            continue
        response = batch.get("response")
        for entry in response.get("shots", []) if isinstance(response, dict) else []:
            if isinstance(entry, dict) and isinstance(entry.get("shotID"), str):
                result[entry["shotID"]] = entry
    return result


def _style_distribution(
    unified: Mapping | None,
    shots: list[dict],
    section: str,
    field: str,
    vocab_field: str,
    vocabulary: Vocabulary,
) -> dict:
    """unified 模型字段经受控词表归一化的分布（按镜头数计权）。

    unknown/unmapped 不入分布（无法归类不编造）；unified 缺失返回空对象。
    """
    if not unified:
        return {}
    model_shots = _model_shots_by_id(unified)
    counts: Counter[str] = Counter()
    for shot in shots:
        model = model_shots.get(str(shot["shotID"]), {})
        section_data = model.get(section)
        raw = section_data.get(field) if isinstance(section_data, dict) else None
        key = vocabulary.canonical_key(vocab_field, raw) if raw is not None else None
        if key:
            counts[key] += 1
    if not counts:
        return {}
    total = sum(counts.values())
    return {key: _ratio(count / total) for key, count in counts.items()}


def _camera_distribution(camera: Mapping | None) -> dict:
    """camera-motion.json 的 Apple Vision 确定性分布（保留原始枚举）。

    不把 pan_left/zoom_in 等映射为摄影术语（docs/02 §4.8：图像运动证据，
    UI 不得夸大为确定摄影术语）；capabilityStatus 非 complete 时空分布。
    """
    if not camera:
        return {}
    analysis = camera.get("analysis")
    if not isinstance(analysis, dict) or analysis.get("capabilityStatus") != "complete":
        return {}
    counts: Counter[str] = Counter()
    for entry in camera.get("shots", []):
        if not isinstance(entry, dict):
            continue
        movement = entry.get("cameraMovement")
        if isinstance(movement, str) and movement and movement != "unknown":
            counts[movement] += 1
    if not counts:
        return {}
    total = sum(counts.values())
    return {key: _ratio(count / total) for key, count in counts.items()}


def _interval_union_ms(
    intervals: Iterable[tuple[int, int]],
    clip: tuple[int, int] | None = None,
) -> int:
    """返回毫秒区间并集时长；可选裁剪到分析范围。

    coverage 字段按“时间并集 / 分析范围”计算，因此不能简单累加 shot 或
    ASR segment 时长：重叠段会双计，越过 analyzedRange 的段也会污染比例。
    """
    normalized: list[tuple[int, int]] = []
    for start, end in intervals:
        if clip is not None:
            start = max(start, clip[0])
            end = min(end, clip[1])
        if end > start:
            normalized.append((start, end))
    if not normalized:
        return 0
    normalized.sort()
    merged: list[tuple[int, int]] = []
    for start, end in normalized:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return sum(end - start for start, end in merged)


def _shot_interval(shot: Mapping) -> tuple[int, int] | None:
    start = _as_int(shot.get("finalStartMs"))
    end = _as_int(shot.get("finalEndMs"))
    if start is not None and end is not None and end > start:
        return start, end
    return None


def _bounded_coverage_ratio(covered_ms: float, total_ms: int) -> dict:
    if total_ms <= 0:
        return {}
    ratio = max(0.0, min(1.0, covered_ms / total_ms))
    return {"coverage": _ratio(ratio)}


def _text_overlay_coverage(
    unified: Mapping | None,
    shots: list[dict],
    total_ms: int,
    analyzed_range: tuple[int, int] | None,
) -> dict:
    """components.texts 非空的镜头时长并集占分析范围比例。"""
    if not unified or not shots:
        return {}
    model_shots = _model_shots_by_id(unified)
    intervals: list[tuple[int, int]] = []
    for shot in shots:
        model = model_shots.get(str(shot["shotID"]), {})
        components = model.get("components")
        texts = components.get("texts") if isinstance(components, dict) else None
        has_text = isinstance(texts, list) and any(
            isinstance(t, dict) and isinstance(t.get("textContent"), str)
            and t["textContent"].strip()
            for t in texts
        )
        if has_text:
            interval = _shot_interval(shot)
            if interval is not None:
                intervals.append(interval)
    covered = _interval_union_ms(intervals, analyzed_range)
    return _bounded_coverage_ratio(covered, total_ms)


def _bgm_coverage(
    music: Mapping | None,
    shots: list[dict],
    total_ms: int,
    analyzed_range: tuple[int, int] | None,
) -> dict:
    """BGM coverage：优先使用 musicIntervals 的时间并集占分析范围比例。"""
    if not music or music.get("status") != "complete" or not shots:
        return {}
    interval_inputs: list[tuple[int, int]] = []
    for interval in music.get("musicIntervals", []):
        if not isinstance(interval, dict):
            continue
        start = interval.get("startSec")
        end = interval.get("endSec")
        if (
            isinstance(start, (int, float))
            and isinstance(end, (int, float))
            and not isinstance(start, bool)
            and not isinstance(end, bool)
        ):
            interval_inputs.append((int(round(start * 1000)), int(round(end * 1000))))
    if interval_inputs:
        covered = _interval_union_ms(interval_inputs, analyzed_range)
        return _bounded_coverage_ratio(covered, total_ms)

    ratios_by_shot: dict[str, float] = {}
    for entry in music.get("shots", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("shotID"), str):
            continue
        ratio = entry.get("musicOverlapRatio")
        if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
            continue
        sid = entry["shotID"]
        ratios_by_shot[sid] = max(ratios_by_shot.get(sid, 0.0), max(0.0, min(1.0, float(ratio))))
    covered = 0.0
    for sid, ratio in ratios_by_shot.items():
        shot = _shots_by_id(shots).get(sid)
        interval = _shot_interval(shot) if shot is not None else None
        if interval is None:
            continue
        covered += _interval_union_ms([interval], analyzed_range) * ratio
    return _bounded_coverage_ratio(covered, total_ms)


def _speech_coverage(
    asr: Mapping | None,
    total_ms: int,
    analyzed_range: tuple[int, int] | None,
) -> dict:
    """voiceMix.speechCoverage：ASR 语音时长并集占分析范围比例。"""
    if not asr or asr.get("status") != "complete":
        return {}
    speech_ms = _interval_union_ms(
        ((s["startMs"], s["endMs"]) for s in _asr_segments(asr)),
        analyzed_range,
    )
    if total_ms <= 0:
        return {}
    ratio = max(0.0, min(1.0, speech_ms / total_ms))
    return {"speechCoverage": _ratio(ratio)}


def _hosted_coverage(
    unified: Mapping | None,
    shots: list[dict],
    total_ms: int,
    analyzed_range: tuple[int, int] | None,
) -> float:
    """hostedCoverage：subjects 明确含人物关键词的镜头时长占比。

    无法可靠判断时返回 0.0（保守可解释值，docs/06 A-007）；unified 缺失
    同样 0.0——不编造出镜证据。
    """
    if not unified or not shots:
        return 0.0
    model_shots = _model_shots_by_id(unified)
    intervals: list[tuple[int, int]] = []
    for shot in shots:
        model = model_shots.get(str(shot["shotID"]), {})
        visual = model.get("visual")
        subjects = visual.get("subjects") if isinstance(visual, dict) else None
        if isinstance(subjects, str) and any(
            keyword in subjects for keyword in HOSTED_KEYWORDS
        ):
            interval = _shot_interval(shot)
            if interval is not None:
                intervals.append(interval)
    if total_ms <= 0:
        return 0.0
    covered = _interval_union_ms(intervals, analyzed_range)
    return _ratio(max(0.0, min(1.0, covered / total_ms)))


def _style(
    unified: Mapping | None,
    camera: Mapping | None,
    music: Mapping | None,
    asr: Mapping | None,
    shots: list[dict],
    total_ms: int,
    analyzed_range: tuple[int, int] | None,
    vocabulary: Vocabulary,
) -> dict:
    style: dict[str, dict | float] = {}
    for key, (section, field, vocab_field) in _DISTRIBUTION_FIELDS.items():
        style[key] = _style_distribution(
            unified, shots, section, field, vocab_field, vocabulary
        )
    style["cameraMovement"] = _camera_distribution(camera)
    style["textOverlay"] = _text_overlay_coverage(unified, shots, total_ms, analyzed_range)
    style["bgm"] = _bgm_coverage(music, shots, total_ms, analyzed_range)
    style["voiceMix"] = _speech_coverage(asr, total_ms, analyzed_range)
    style["hostedCoverage"] = _hosted_coverage(unified, shots, total_ms, analyzed_range)
    return style


# ---------------------------------------------------------------------------
# ASR 文本统计
# ---------------------------------------------------------------------------


def _asr_segments(asr: Mapping | None) -> list[dict]:
    if not asr or asr.get("status") != "complete":
        return []
    transcript = asr.get("transcript")
    segments = transcript.get("segments") if isinstance(transcript, dict) else None
    if not isinstance(segments, list):
        return []
    return [
        s for s in segments
        if isinstance(s, dict)
        and isinstance(s.get("startMs"), int)
        and isinstance(s.get("endMs"), int)
        and s["endMs"] > s["startMs"]
    ]


def _asr_speech_duration_ms(asr: Mapping | None) -> int:
    return sum(s["endMs"] - s["startMs"] for s in _asr_segments(asr))


def _asr_text_stats(asr: Mapping | None) -> dict:
    segments = _asr_segments(asr)
    return {
        "segmentCount": len(segments),
        "characterCount": sum(
            len(s["text"]) for s in segments if isinstance(s.get("text"), str)
        ),
        "speechDurationMs": _asr_speech_duration_ms(asr),
    }


# ---------------------------------------------------------------------------
# source
# ---------------------------------------------------------------------------


def _source(media: Mapping | None) -> dict:
    src = media.get("source", {}) if isinstance(media, dict) else {}
    duration_ms = _as_int(src.get("durationMs"))
    return {
        "videoTitle": src.get("assetID") if isinstance(src.get("assetID"), str) else None,
        "videoPath": src.get("sourcePath") if isinstance(src.get("sourcePath"), str) else None,
        "durationSeconds": round(duration_ms / 1000.0, 3) if duration_ms is not None else 0.0,
        "platform": None,
        "formType": None,
        "shotAnalysisPath": "shot-analysis.html",
        "storyAnalysisPath": "story-analysis.html",
        "sourceRevision": src.get("revisionID") if isinstance(src.get("revisionID"), str) else None,
    }


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_profile_aggregate(
    raws: Mapping[str, Mapping | None],
    *,
    generated_at: str | None = None,
    vocabulary: Vocabulary | None = None,
) -> dict:
    """从稳定 JSON 构造 style-profile 确定性聚合文档（纯函数，不写盘）。

    ``raws`` 的键为逻辑名（media/shots/asr/music-flags/audio-cuts/
    camera-motion/unified-media/story-blocks），缺失或不可用传 None。

    :raises ValueError: media/shots/story-blocks 缺失或不可用（profile 的
        确定性部分依赖镜头与故事契约）。
    """
    media = raws.get("media")
    shots_doc = raws.get("shots")
    story = raws.get("story-blocks")
    vocabulary = vocabulary or load_vocabulary()
    if not shots_doc or not story:
        raise ValueError("profile 聚合需要可用的 raw/shots.json 与 raw/story-blocks.json")
    if media is None:
        raise ValueError("profile 聚合需要可用的 raw/media.json")

    shots = shots_sorted(shots_doc)
    if not shots:
        raise ValueError("raw/shots.json 不含合法镜头条目")
    shots_by_id = _shots_by_id(shots)
    blocks_by_id = _blocks_by_id(story)
    if not blocks_by_id:
        raise ValueError("raw/story-blocks.json 不含合法 block")

    analyzed = _analyzed_range_ms(media)
    total_ms = analyzed[1] - analyzed[0] if analyzed else _total_duration_ms(shots)
    if total_ms <= 0:
        raise ValueError("分析范围或镜头总时长无效")

    if generated_at is None:
        generated_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )

    slots_raw = story.get("slots")
    slots = (
        [s for s in slots_raw if isinstance(s, dict) and isinstance(s.get("slotID"), str)]
        if isinstance(slots_raw, list)
        else []
    )
    blocks = list(blocks_by_id.values())

    revision = media.get("source", {}).get("revisionID")
    revision = revision if isinstance(revision, str) else "unknown"

    return {
        "schemaVersion": SCHEMA_VERSION,
        "id": f"profile-{revision}",
        "createdAt": generated_at,
        "source": _source(media),
        "structure": _structure(story, blocks_by_id, shots_by_id, total_ms),
        "pacing": _pacing(
            slots, blocks_by_id, shots_by_id, shots,
            raws.get("audio-cuts"), raws.get("music-flags"), blocks,
        ),
        "style": _style(
            raws.get("unified-media"), raws.get("camera-motion"),
            raws.get("music-flags"), raws.get("asr"), shots, total_ms, analyzed,
            vocabulary,
        ),
        "structureRequirements": [],
        "adoptionHints": None,
        "discussionItems": [],
        "asrTextStats": _asr_text_stats(raws.get("asr")),
        "distillStatus": "skipped",
    }
