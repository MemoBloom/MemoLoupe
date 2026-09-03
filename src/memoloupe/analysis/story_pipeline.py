"""analysis.story_pipeline — Phase 2 故事分析（docs/03 §3、roadmap 03-02/03-03）。

确定性 scaffold（03-02）：
聚块算法（docs/03 §3.2，全程确定性）：

1. ASR segments 按 ``(startMs, endMs)`` 排序，相邻间隔 ``>= gapMs``（默认 2000）
   切开成 speech run（停顿段）；
2. :func:`segment_of` 为镜头定主停顿段：取时间交集最大的 run；零重叠时归入
   ``endMs <= startMs`` 的最晚 run（尾部静默跟随上一段），没有更早 run 时归入
   最早的 run（片头静默并入首块）；无 run 返回 sentinel ``-1``；
3. 顺序遍历镜头，段号变化开新 block；``current_seg`` 初值 ``None`` 保证首镜头
   强制开块；
4. block start/end 从首尾镜头 final 边界派生；shotIDs 保持镜头顺序、全量覆盖；
5. 无 ASR（缺失/skipped/failed/空 segments）时保守单块，不猜测视觉故事边界。

文本模型编排（03-03）：

- ``text_service`` 为 None 时只产 scaffold（``status=scaffold``）；
- 模型请求只发送 :mod:`memoloupe.analysis.story_prompts` 渲染的文本摘要，
  绝不包含视频/帧/Data URI/路径；
- 响应解析失败、ID 集合不闭合、试图改 shot 归属/边界、schema 不合等任何
  不合规都整体回退到 scaffold——候选 blocks 绝不因模型失败丢失；
- 每次成功请求后写 ``checkpoints/story-blocks-model.json``；重跑指纹命中时
  直接复用，不再请求模型；
- 确定性字段（storyBlockID/shotIDs/startMs/endMs/boundary）结构上只从
  scaffold 复制，模型无权覆盖。

scaffold 叙事字段一律合法占位：枚举落 ``unknown``，自由文本落空字符串，
``status=scaffold``、``boundarySource=asr-gap``、``slots=[]``（slot 聚合是模型职责）。

复用：scaffold 指纹 = shots/asr/unified-media 内容哈希 + gapMs + 实现版本；
模型填充指纹在此基础上叠加 prompt 版本与服务标记。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from memoloupe.analysis.story_prompts import (
    AUDIENCE_REACTIONS,
    BLOCK_TITLE_MAX,
    DIVISION_AXES,
    INFORMATION_ROLES,
    NARRATIVE_DENSITIES,
    PRIMARY_ROLES,
    SLOT_TITLE_MAX,
    SLOT_TYPES,
    STORY_PROMPT_VERSION,
    VISUAL_INDEPENDENCES,
    build_story_prompt,
)
from memoloupe.analysis.vocabulary import load_vocabulary
from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
from memoloupe.artifacts.store import ArtifactStore, WriteMetadata
from memoloupe.core.atomic_io import read_json, write_json_atomic
from memoloupe.core.errors import MemoLoupeError
from memoloupe.core.hashing import content_revision_id, fingerprint
from memoloupe.core.logging import get_logger, log_step
from memoloupe.services.base import ServiceError
from memoloupe.services.text_model import TextModelRequest

# 围栏剥离复用统一模型编排器的实现（包内私有，保持单一实现）。
from memoloupe.analysis.media_orchestrator import _strip_single_fence

# PipelineReport/StepRecord 复用 Phase 1 的公共报告类型；_Lock 是包内私有的
# output-dir 写锁实现（docs/03 §6 同一 output-dir 单写入者），避免复制。
from memoloupe.analysis.shot_pipeline import (  # noqa: F401
    PipelineReport,
    StepRecord,
    _Lock as _OutputLock,
)

STORY_SCAFFOLD_VERSION = "story-scaffold.v1"
STORY_MODEL_FILL_VERSION = "story-model-fill.v1"

#: 无 speech run 时 segment_of 的 sentinel（不会与合法段号冲突）。
NO_RUN = -1

#: 模型叙事字段的 scaffold 占位：枚举 unknown、自由文本空字符串（docs/03 §3.3）。
_SCAFFOLD_NARRATIVE_DEFAULTS: dict[str, str] = {
    "divisionAxis": "unknown",
    "divisionRationale": "",
    "primaryRole": "unknown",
    "coreContent": "",
    "informationRole": "unknown",
    "narrativeDensity": "unknown",
    "audienceReaction": "unknown",
    "visualIndependence": "unknown",
    "blockRelation": "",
    "relationReason": "",
}


@dataclass(frozen=True)
class StoryAnalysisRequest:
    """故事分析请求。``allow_draft`` 由 03-04 CLI 层消费（草稿输入门禁）。"""

    output_dir: Path
    gap_ms: int = 2000
    allow_draft: bool = False
    text_service: Any = None  # TextModelService；None 时只产 scaffold
    force: frozenset[str] = frozenset()
    no_cache: bool = False
    # 05-04：调试模式——只保留前 N 个 block（产物不满足全覆盖契约，见 warning）。
    max_blocks: int | None = None


# ---------------------------------------------------------------------------
# 镜头文本摘要（docs/03 §3.1；03-03 的 prompt 输入）
# ---------------------------------------------------------------------------

#: 摘要的视觉白名单字段：只复制文本语义，绝不携带路径/二进制/引用。
_SUMMARY_VISUAL_FIELDS = ("subjects", "actions", "setting", "props")


def _content_summary(visual: Mapping[str, object]) -> str:
    """从原子视觉语义生成稳定摘要；忽略空值、unknown 与模型缺席声称。"""
    values: list[str] = []
    for field in _SUMMARY_VISUAL_FIELDS:
        value = visual.get(field)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if text and text.casefold() not in {"unknown", "无", "没有", "none"}:
            values.append(text)
    return "；".join(values)


def _transition_from_boundary(shot: Mapping[str, object]) -> str:
    """从镜头入边界派生转场；当前确定性检测只授权确认硬切。"""
    boundary = shot.get("boundaryIn")
    boundary_type = boundary.get("type") if isinstance(boundary, dict) else None
    return "硬切" if boundary_type == "hardCutCandidate" else ""


def _model_shots_by_id(unified: dict | None) -> dict[str, dict]:
    """unified-media 的模型镜头按 shotID 索引（绝不按下标对齐，docs/04 §8.5）。"""
    result: dict[str, dict] = {}
    if not unified:
        return result
    batches = unified.get("batches")
    if not isinstance(batches, list):
        return result
    for batch in batches:
        if not isinstance(batch, dict):
            continue
        response = batch.get("response")
        shots = response.get("shots") if isinstance(response, dict) else None
        if not isinstance(shots, list):
            continue
        for entry in shots:
            if isinstance(entry, dict) and isinstance(entry.get("shotID"), str):
                result[entry["shotID"]] = entry
    return result


def _shot_speech(asr: dict | None, start_ms: int, end_ms: int) -> str:
    """镜头 final 区间内的 ASR 文本，按时间顺序拼接。"""
    if not asr or asr.get("status") != "complete":
        return ""
    transcript = asr.get("transcript")
    segments = transcript.get("segments") if isinstance(transcript, dict) else None
    if not isinstance(segments, list):
        return ""
    texts = [
        str(seg["text"])
        for seg in sorted(
            (
                s for s in segments
                if isinstance(s, dict)
                and isinstance(s.get("startMs"), int)
                and isinstance(s.get("endMs"), int)
                and isinstance(s.get("text"), str)
            ),
            key=lambda s: (s["startMs"], s["endMs"]),
        )
        if seg["startMs"] < end_ms and seg["endMs"] > start_ms
    ]
    return " ".join(texts)


def build_shot_summaries(raws: Mapping[str, dict | None]) -> list[dict]:
    """从稳定 JSON 构造每镜头文本摘要（docs/03 §3.1）。

    白名单复制：shotID/final 时间、原子视觉语义及其派生 contentSummary、
    ASR speech、text overlays、shots 边界派生 transition、camera-motion 分类。
    结构上不接触 clip 路径、帧引用、Data URI、源视频路径或模型代理路径。
    """
    shots_doc = raws.get("shots")
    shots = shots_doc.get("shots") if isinstance(shots_doc, dict) else None
    if not isinstance(shots, list):
        return []
    model_shots = _model_shots_by_id(raws.get("unified-media"))
    camera_doc = raws.get("camera-motion")
    camera_by_id = {
        entry["shotID"]: entry
        for entry in (camera_doc.get("shots") if isinstance(camera_doc, dict) else [])
        or []
        if isinstance(entry, dict) and isinstance(entry.get("shotID"), str)
    }
    ordered = sorted(
        (s for s in shots if isinstance(s, dict) and isinstance(s.get("shotID"), str)),
        key=lambda s: (s.get("finalStartMs") is None, s.get("finalStartMs", 0)),
    )
    summaries: list[dict] = []
    for shot in ordered:
        shot_id = str(shot["shotID"])
        start_ms = int(shot.get("finalStartMs", 0))
        end_ms = int(shot.get("finalEndMs", 0))
        model = model_shots.get(shot_id, {})
        visual_raw = model.get("visual")
        visual_raw = visual_raw if isinstance(visual_raw, dict) else {}
        components_raw = model.get("components")
        components_raw = components_raw if isinstance(components_raw, dict) else {}
        texts_raw = components_raw.get("texts")
        texts = [
            str(t["textContent"])
            for t in (texts_raw if isinstance(texts_raw, list) else [])
            if isinstance(t, dict) and isinstance(t.get("textContent"), str)
        ]
        camera = camera_by_id.get(shot_id, {})
        summaries.append(
            {
                "shotID": shot_id,
                "startMs": start_ms,
                "endMs": end_ms,
                "visual": {
                    "contentSummary": _content_summary(visual_raw),
                    **{
                        field: str(visual_raw.get(field, ""))
                        for field in _SUMMARY_VISUAL_FIELDS
                    },
                },
                "speech": _shot_speech(raws.get("asr"), start_ms, end_ms),
                "texts": texts,
                "editing": {
                    "transition": _transition_from_boundary(shot),
                },
                "cameraMovement": str(camera.get("cameraMovement", "unknown")),
            }
        )
    return summaries


# ---------------------------------------------------------------------------
# 确定性聚块（docs/03 §3.2）
# ---------------------------------------------------------------------------


def compute_speech_runs(segments: Iterable[dict], gap_ms: int) -> list[dict]:
    """把 ASR segments 按停顿阈值合并为 speech run（停顿段）。

    相邻 segment 间隔 ``>= gap_ms`` 切开；重叠/乱序输入先排序再合并。
    run 区间为 ``[首 segment startMs, 末 segment endMs]``。
    """
    ordered = sorted(
        (
            s for s in segments
            if isinstance(s, dict)
            and isinstance(s.get("startMs"), int)
            and isinstance(s.get("endMs"), int)
        ),
        key=lambda s: (s["startMs"], s["endMs"]),
    )
    runs: list[dict] = []
    for seg in ordered:
        if runs and seg["startMs"] - runs[-1]["endMs"] >= gap_ms:
            runs.append(
                {"startMs": seg["startMs"], "endMs": seg["endMs"], "segmentCount": 1}
            )
        elif runs:
            runs[-1]["endMs"] = max(runs[-1]["endMs"], seg["endMs"])
            runs[-1]["segmentCount"] += 1
        else:
            runs.append(
                {"startMs": seg["startMs"], "endMs": seg["endMs"], "segmentCount": 1}
            )
    return runs


def segment_of(runs: list[dict], start_ms: int, end_ms: int) -> int:
    """镜头 [startMs, endMs) 的主停顿段号；无 run 返回 ``NO_RUN``。

    规则：时间交集最大的 run（并列取最早）；零重叠时归入 ``endMs <= startMs``
    的最晚 run；没有更早 run 时归入最早的 run（片头静默并入首块）。
    """
    if not runs:
        return NO_RUN
    best_index, best_overlap = 0, -1
    for index, run in enumerate(runs):
        overlap = max(0, min(end_ms, run["endMs"]) - max(start_ms, run["startMs"]))
        if overlap > best_overlap:
            best_index, best_overlap = index, overlap
    if best_overlap > 0:
        return best_index
    preceding = [i for i, run in enumerate(runs) if run["endMs"] <= start_ms]
    return preceding[-1] if preceding else 0


def _scaffold_block(block_index: int, shots: list[dict], has_runs: bool, gap_ms: int) -> dict:
    shot_ids = [str(s["shotID"]) for s in shots]
    if block_index == 1:
        boundary = (
            {
                "level": "start",
                "signal": "sourceStart",
                "label": "片头（首镜头强制开块）",
            }
            if has_runs
            else {
                "level": "start",
                "signal": "none",
                "label": "无 ASR 语音，保守单块 scaffold",
            }
        )
    else:
        boundary = {
            "level": "candidate",
            "signal": "asr-gap",
            "label": f"ASR 停顿分段（gapMs={gap_ms}）",
        }
    return {
        "storyBlockID": f"B{block_index:04d}",
        "shotIDs": shot_ids,
        "startMs": int(shots[0]["finalStartMs"]),
        "endMs": int(shots[-1]["finalEndMs"]),
        "boundary": boundary,
        **_SCAFFOLD_NARRATIVE_DEFAULTS,
    }


def build_scaffold_document(
    shots: list[dict],
    asr: dict | None,
    gap_ms: int,
    *,
    generated_at: str | None = None,
) -> dict:
    """构造 story-blocks scaffold 文档（纯函数，不写盘）。

    ``shots`` 为 shots.json 的镜头条目（任意顺序，内部按 finalStartMs 排序）；
    ``asr`` 缺失/非 complete/空 segments 时保守单块。
    """
    ordered = sorted(shots, key=lambda s: (s["finalStartMs"], s["finalEndMs"]))
    segments: list[dict] = []
    if isinstance(asr, dict) and asr.get("status") == "complete":
        transcript = asr.get("transcript")
        if isinstance(transcript, dict) and isinstance(transcript.get("segments"), list):
            segments = transcript["segments"]
    runs = compute_speech_runs(segments, gap_ms)

    blocks: list[dict] = []
    current_shots: list[dict] = []
    current_seg: int | None = None  # sentinel：首镜头强制开块
    for shot in ordered:
        seg = segment_of(runs, int(shot["finalStartMs"]), int(shot["finalEndMs"]))
        if current_seg is None or seg != current_seg:
            if current_shots:
                blocks.append(
                    _scaffold_block(len(blocks) + 1, current_shots, bool(runs), gap_ms)
                )
            current_shots = []
            current_seg = seg
        current_shots.append(shot)
    if current_shots:
        blocks.append(_scaffold_block(len(blocks) + 1, current_shots, bool(runs), gap_ms))

    if generated_at is None:
        generated_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
    return {
        "status": "scaffold",
        "boundarySource": "asr-gap",
        "gapMs": int(gap_ms),
        "generatedAt": generated_at,
        "blocks": blocks,
        "slots": [],
    }


# ---------------------------------------------------------------------------
# 模型响应解析与合并（03-03）
# ---------------------------------------------------------------------------


class InvalidModelResponse(Exception):
    """模型响应不合规：整体回退 scaffold，不做部分采纳。"""


_SLOT_ID_RE = re.compile(r"^S\d{3}$")
_MULTI_SPLIT_RE = re.compile(r"[、，,]")


def _normalize_single_enum(value: object, allowed: tuple[str, ...]) -> str:
    """单值枚举归一化：去空白；不在受控集合落 ``unknown``（不编造语义）。"""
    text = str(value).strip() if value is not None else ""
    return text if text in allowed else "unknown"


def _normalize_multi_enum(value: object, allowed: tuple[str, ...]) -> str:
    """多值枚举归一化：顿号/逗号分隔、去空白、保序去重、滤掉表外值。"""
    text = str(value) if value is not None else ""
    kept: list[str] = []
    for token in _MULTI_SPLIT_RE.split(text):
        token = token.strip()
        if token in allowed and token not in kept:
            kept.append(token)
    return "、".join(kept)


def _text_field(raw: dict, key: str) -> str:
    value = raw.get(key, "")
    return str(value).strip() if value is not None else ""


def parse_model_result(text: str, scaffold_blocks: list[dict]) -> dict:
    """解析并校验模型响应，返回 ``{"blocks": {bid: narrative}, "slots": [...]}``。

    任何不合规抛 :class:`InvalidModelResponse`：非法 JSON、block ID 集合不闭合
    （漏/未知/重复）、试图修改 shot 归属或边界、标题超长、slot 引用未闭合等。
    归一化只做确定性映射（去空白、表外枚举落 unknown、多值过滤），不编造语义。
    """
    stripped = _strip_single_fence(text)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise InvalidModelResponse(f"非法 JSON: {exc.msg}") from None
    if not isinstance(data, dict):
        raise InvalidModelResponse(f"响应不是 JSON 对象: {type(data).__name__}")
    raw_blocks = data.get("blocks")
    raw_slots = data.get("slots", [])
    if not isinstance(raw_blocks, list):
        raise InvalidModelResponse("blocks 缺失或不是数组")
    if not isinstance(raw_slots, list):
        raise InvalidModelResponse("slots 不是数组")

    scaffold_by_id = {b["storyBlockID"]: b for b in scaffold_blocks}
    if not all(isinstance(b, dict) for b in raw_blocks):
        raise InvalidModelResponse("blocks 含非对象条目")
    response_ids = [b.get("storyBlockID") for b in raw_blocks]
    if Counter(response_ids) != Counter(scaffold_by_id.keys()):
        raise InvalidModelResponse(
            f"block ID 集合不一致: scaffold {sorted(scaffold_by_id)} "
            f"响应 {sorted(str(i) for i in response_ids)}"
        )

    blocks: dict[str, dict] = {}
    for raw in raw_blocks:
        block_id = str(raw["storyBlockID"])
        scaffold = scaffold_by_id[block_id]
        # 模型不得新增/删除/重排/重分配 shot，不得改确定性边界。
        if "shotIDs" in raw and raw["shotIDs"] != scaffold["shotIDs"]:
            raise InvalidModelResponse(
                f"{block_id} 试图修改 shot 归属: {raw['shotIDs']}"
            )
        for key in ("startMs", "endMs"):
            if key in raw and raw[key] != scaffold[key]:
                raise InvalidModelResponse(
                    f"{block_id} 试图修改边界 {key}: {raw[key]}"
                )
        narrative: dict[str, str] = {
            "divisionAxis": _normalize_single_enum(raw.get("divisionAxis"), DIVISION_AXES),
            "divisionRationale": _text_field(raw, "divisionRationale"),
            "primaryRole": _normalize_single_enum(raw.get("primaryRole"), PRIMARY_ROLES),
            "coreContent": _text_field(raw, "coreContent"),
            "informationRole": _normalize_multi_enum(
                raw.get("informationRole"), INFORMATION_ROLES
            )
            or "unknown",
            "narrativeDensity": _normalize_single_enum(
                raw.get("narrativeDensity"), NARRATIVE_DENSITIES
            ),
            "audienceReaction": _normalize_single_enum(
                raw.get("audienceReaction"), AUDIENCE_REACTIONS
            ),
            "visualIndependence": _normalize_single_enum(
                raw.get("visualIndependence"), VISUAL_INDEPENDENCES
            ),
            "blockRelation": _text_field(raw, "blockRelation"),
            "relationReason": _text_field(raw, "relationReason"),
        }
        title = _text_field(raw, "blockTitle")
        if len(title) > BLOCK_TITLE_MAX:
            raise InvalidModelResponse(
                f"{block_id} blockTitle 超长: {len(title)} > {BLOCK_TITLE_MAX}"
            )
        if title:
            narrative["blockTitle"] = title
        boundary_basis = _text_field(raw, "boundaryBasis")
        if boundary_basis:
            narrative["boundaryBasis"] = boundary_basis
        blocks[block_id] = narrative

    slots: list[dict] = []
    seen_slot_ids: set[str] = set()
    covered_block_ids: set[str] = set()
    for raw_slot in raw_slots:
        if not isinstance(raw_slot, dict):
            raise InvalidModelResponse("slots 含非对象条目")
        slot_id = _text_field(raw_slot, "slotID")
        if not _SLOT_ID_RE.match(slot_id):
            raise InvalidModelResponse(f"非法 slotID: {slot_id!r}")
        if slot_id in seen_slot_ids:
            raise InvalidModelResponse(f"重复 slotID: {slot_id}")
        seen_slot_ids.add(slot_id)
        slot_type = _normalize_multi_enum(raw_slot.get("slotType"), SLOT_TYPES)
        if not slot_type:
            raise InvalidModelResponse(f"{slot_id} slotType 无合法取值")
        slot_title = _text_field(raw_slot, "slotTitle")
        if len(slot_title) > SLOT_TITLE_MAX:
            raise InvalidModelResponse(
                f"{slot_id} slotTitle 超长: {len(slot_title)} > {SLOT_TITLE_MAX}"
            )
        raw_ids = raw_slot.get("blockIDs")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise InvalidModelResponse(f"{slot_id} blockIDs 缺失或为空")
        block_ids = [str(b) for b in raw_ids]
        unknown = [b for b in block_ids if b not in scaffold_by_id]
        if unknown:
            raise InvalidModelResponse(
                f"{slot_id} 引用未知 block: {sorted(unknown)}"
            )
        deduped = list(dict.fromkeys(block_ids))
        covered_block_ids.update(deduped)
        slots.append(
            {
                "slotID": slot_id,
                "slotType": slot_type,
                "slotTitle": slot_title,
                "blockIDs": deduped,
                "slotRationale": _text_field(raw_slot, "slotRationale"),
            }
        )
    missing = sorted(set(scaffold_by_id) - covered_block_ids)
    if missing:
        raise InvalidModelResponse(
            f"complete 模型响应未把全部 block 分配到 slot: {missing}"
        )
    return {"blocks": blocks, "slots": slots}


def merge_model_result(
    scaffold_doc: dict, result: dict, *, generated_at: str
) -> dict:
    """把模型叙事字段合并到 scaffold 上。

    确定性字段（storyBlockID/shotIDs/startMs/endMs/boundary）只从 scaffold
    复制——模型在结构上无权覆盖；``boundarySource`` 保持 ``asr-gap``。
    """
    blocks = []
    for block in scaffold_doc["blocks"]:
        merged = dict(block)
        merged.update(result["blocks"][block["storyBlockID"]])
        blocks.append(merged)
    return {
        "status": "complete",
        "boundarySource": scaffold_doc["boundarySource"],
        "gapMs": scaffold_doc["gapMs"],
        "generatedAt": generated_at,
        "blocks": blocks,
        "slots": result["slots"],
    }


# ---------------------------------------------------------------------------
# 模型填充 checkpoint（与最终 artifact 分离，docs/03 §5.2）
# ---------------------------------------------------------------------------


def _model_checkpoint_path(store: ArtifactStore) -> Path:
    return store.root / "checkpoints" / "story-blocks-model.json"


def _write_model_checkpoint(
    store: ArtifactStore, fp: str, generated_at: str, result: dict
) -> None:
    write_json_atomic(
        _model_checkpoint_path(store),
        {
            "version": STORY_MODEL_FILL_VERSION,
            "fingerprint": fp,
            "generatedAt": generated_at,
            "result": result,
        },
    )


def _load_model_checkpoint(
    store: ArtifactStore, fp: str
) -> tuple[dict, str] | None:
    """加载模型填充 checkpoint；指纹/版本不匹配或文件损坏时返回 None。"""
    try:
        data = read_json(_model_checkpoint_path(store))
    except (MemoLoupeError, OSError):
        return None
    if (
        data.get("version") != STORY_MODEL_FILL_VERSION
        or data.get("fingerprint") != fp
        or not isinstance(data.get("result"), dict)
        or not isinstance(data.get("generatedAt"), str)
    ):
        return None
    return data["result"], data["generatedAt"]


# ---------------------------------------------------------------------------
# 编排器
# ---------------------------------------------------------------------------


def _elapsed(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _file_revision(store: ArtifactStore, name: ArtifactName) -> str:
    """产物文件内容哈希；文件不存在时为 ``none``（进入指纹，不含路径）。"""
    path = store.path(name)
    return content_revision_id(path) if path.exists() else "none"


def _story_slot_ids(doc: dict) -> set[str]:
    return {
        slot["slotID"]
        for slot in doc.get("slots", [])
        if isinstance(slot, dict) and isinstance(slot.get("slotID"), str)
    }


def _profile_slot_ids(doc: dict) -> set[str]:
    slots = doc.get("structure", {}).get("slots", [])
    if not isinstance(slots, list):
        return set()
    return {
        slot["slotId"]
        for slot in slots
        if isinstance(slot, dict) and isinstance(slot.get("slotId"), str)
    }


def _archive_style_profile_if_needed(
    out_dir: Path,
    story_doc: dict,
    *,
    story_rewritten: bool,
) -> str | None:
    """把失效的 active style-profile 移到 checkpoints/outdated，返回相对路径。

    Phase 3 profile 是 story 的下游产物。story 被重写时无法证明旧 profile
    仍然对应当前叙事语义；即使 story 未重写，slot 集合已不一致也必须移出
    active contract 路径，避免 strict validate 读取陈旧 profile 后失败。
    """
    profile_path = out_dir / "style-profile.json"
    if not profile_path.exists():
        return None
    should_archive = story_rewritten
    if not should_archive:
        try:
            profile_doc = read_json(profile_path)
        except MemoLoupeError:
            should_archive = True
        else:
            should_archive = _profile_slot_ids(profile_doc) != _story_slot_ids(story_doc)
    if not should_archive:
        return None

    archive_dir = out_dir / "checkpoints" / "outdated"
    archive_dir.mkdir(parents=True, exist_ok=True)
    try:
        digest = content_revision_id(profile_path)
    except OSError:
        digest = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = archive_dir / f"style-profile.{digest}.json"
    counter = 1
    while target.exists():
        target = archive_dir / f"style-profile.{digest}.{counter}.json"
        counter += 1
    profile_path.replace(target)
    return str(target.relative_to(out_dir))


class StoryAnalysisPipeline:
    """Phase 2 故事分析编排器（确定性 scaffold + 可选文本模型填充）。"""

    def run(self, request: StoryAnalysisRequest) -> PipelineReport:
        started = time.monotonic()
        run_id = uuid.uuid4().hex[:8]
        logger = get_logger(
            "memoloupe.analysis.story_pipeline", run_id=run_id, phase="story"
        )
        out_dir = Path(request.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        store = ArtifactStore(out_dir)
        steps: list[StepRecord] = []
        warnings: list[str] = []

        def record(name: str, status: str, elapsed_ms: int, detail: str | None = None) -> None:
            log_step(logger, name, status, elapsed_ms, **({"detail": detail} if detail else {}))
            steps.append(StepRecord(name=name, status=status, elapsed_ms=elapsed_ms, detail=detail))

        def archive_profile(story_doc: dict, *, story_rewritten: bool) -> None:
            step = time.monotonic()
            archived = _archive_style_profile_if_needed(
                out_dir, story_doc, story_rewritten=story_rewritten
            )
            if archived is None:
                return
            warnings.append(
                f"style-profile.json 已因 story-blocks 更新或 slot 不一致归档到 {archived}"
            )
            record("invalidate_style_profile", "complete", _elapsed(step), detail=archived)

        def report(status: str) -> PipelineReport:
            return PipelineReport(
                phase="story",
                status=status,
                steps=steps,
                warnings=warnings,
                artifacts=(
                    ["raw/story-blocks.json"]
                    if store.exists(ArtifactName.STORY_BLOCKS)
                    else []
                ),
                elapsed_ms=_elapsed(started),
            )

        lock = _OutputLock(out_dir / ".memoloupe.lock", run_id)
        step_start = time.monotonic()
        conflict = lock.acquire()
        if conflict is not None:
            record("acquire_lock", "failed", _elapsed(step_start), detail=conflict)
            warnings.append(conflict)
            return report("failed")
        record("acquire_lock", "complete", _elapsed(step_start), detail=f"runID={run_id}")

        try:
            # 1. load_inputs：media/shots 必需；asr/unified-media 缺失显式降级。
            step_start = time.monotonic()
            try:
                store.read(ArtifactName.MEDIA)
                shots_doc = store.read(ArtifactName.SHOTS)
            except MemoLoupeError as exc:
                record("load_inputs", "failed", _elapsed(step_start), detail=str(exc))
                warnings.append(f"故事分析输入不可用：{exc}")
                return report("failed")
            shots = [
                s
                for s in shots_doc.get("shots", [])
                if isinstance(s, dict)
                and isinstance(s.get("shotID"), str)
                and isinstance(s.get("finalStartMs"), int)
                and isinstance(s.get("finalEndMs"), int)
            ]
            if not shots:
                record("load_inputs", "failed", _elapsed(step_start),
                       detail="shots.json 不含合法镜头条目")
                warnings.append("raw/shots.json 不含合法镜头条目，无法聚块")
                return report("failed")
            raws: dict[str, dict | None] = {"shots": shots_doc}
            for name in (ArtifactName.ASR, ArtifactName.UNIFIED_MEDIA):
                try:
                    raws[name.value] = store.read(name)
                except MemoLoupeError:
                    raws[name.value] = None
                    warnings.append(f"{name.value}.json 缺失或非法，按降级处理")
            record("load_inputs", "complete", _elapsed(step_start),
                   detail=f"shots={len(shots)}")

            # 2. scaffold_story_blocks：指纹复用或重建。
            step_start = time.monotonic()
            fp = fingerprint(
                {
                    "artifact": "story-blocks",
                    "shots": _file_revision(store, ArtifactName.SHOTS),
                    "asr": _file_revision(store, ArtifactName.ASR),
                    "unifiedMedia": _file_revision(store, ArtifactName.UNIFIED_MEDIA),
                    "gapMs": request.gap_ms,
                    "version": STORY_SCAFFOLD_VERSION,
                }
            )
            # 模型填充指纹：scaffold 指纹 + prompt 版本 + 词表版本 + 服务标记。
            model_fp = fingerprint(
                {
                    "artifact": "story-blocks-model",
                    "scaffold": fp,
                    "promptVersion": STORY_PROMPT_VERSION,
                    "vocabVersion": load_vocabulary().version,
                    "service": "injected" if request.text_service is not None else "none",
                    "version": STORY_MODEL_FILL_VERSION,
                }
            )
            cacheable = not request.no_cache and "scaffold_story_blocks" not in request.force
            fill_cacheable = not request.no_cache and "story_model_fill" not in request.force
            # 快速路径：artifact 已是当前配置下的模型填充产物，整段复用。
            if (
                request.text_service is not None
                and cacheable
                and fill_cacheable
                and store.is_reusable(ArtifactName.STORY_BLOCKS, model_fp)
            ):
                record("scaffold_story_blocks", "reused", _elapsed(step_start),
                       detail=f"fingerprint={fp}")
                record("story_model_fill", "reused", 0, detail=f"fingerprint={model_fp}")
                try:
                    archive_profile(
                        store.read(ArtifactName.STORY_BLOCKS), story_rewritten=False
                    )
                except MemoLoupeError:
                    pass
                return report("complete")
            try:
                summaries = build_shot_summaries(raws)  # 模型 prompt 输入
                scaffold_doc = build_scaffold_document(
                    shots, raws.get("asr"), request.gap_ms
                )
                # 05-04：调试模式 --max-blocks——只保留前 N 个 block。
                if request.max_blocks is not None and len(scaffold_doc["blocks"]) > request.max_blocks:
                    scaffold_doc["blocks"] = scaffold_doc["blocks"][: request.max_blocks]
                    scaffold_doc["slots"] = []
                    warnings.append(
                        f"调试模式 --max-blocks={request.max_blocks}：仅保留前 "
                        f"{len(scaffold_doc['blocks'])} 个 block；产物不满足全量覆盖契约，"
                        "validate 预期报错"
                    )
                scaffold_rewritten = False
                if cacheable and store.is_reusable(ArtifactName.STORY_BLOCKS, fp):
                    record("scaffold_story_blocks", "reused", _elapsed(step_start),
                           detail=f"fingerprint={fp}")
                else:
                    store.write(
                        ArtifactName.STORY_BLOCKS,
                        scaffold_doc,
                        WriteMetadata(fingerprint=fp),
                    )
                    scaffold_rewritten = True
                    record("scaffold_story_blocks", "complete", _elapsed(step_start),
                           detail=f"blocks={len(scaffold_doc['blocks'])}")
            except Exception as exc:
                record("scaffold_story_blocks", "failed", _elapsed(step_start),
                       detail=str(exc))
                warnings.append(f"步骤 scaffold_story_blocks 失败：{exc}")
                return report("failed")
            if request.text_service is None:
                archive_profile(scaffold_doc, story_rewritten=scaffold_rewritten)
                return report("complete")

            # 3. story_model_fill：checkpoint 复用或请求模型；任何失败保留
            #    scaffold（候选 blocks 不丢，docs/03 §3.3）。
            step_start = time.monotonic()
            try:
                cached = (
                    _load_model_checkpoint(store, model_fp) if fill_cacheable else None
                )
                if cached is not None:
                    result, generated_at = cached
                    fill_status = "reused"
                else:
                    prompt = build_story_prompt(
                        summaries, scaffold_doc["blocks"], gap_ms=request.gap_ms
                    )
                    text = request.text_service.generate(
                        TextModelRequest(task="story-narrative", prompt=prompt)
                    )
                    result = parse_model_result(text, scaffold_doc["blocks"])
                    generated_at = datetime.now(UTC).isoformat(
                        timespec="milliseconds"
                    ).replace("+00:00", "Z")
                    # 每次成功请求后立即 checkpoint（原子写）。
                    _write_model_checkpoint(store, model_fp, generated_at, result)
                    fill_status = "complete"
                merged = merge_model_result(
                    scaffold_doc, result, generated_at=generated_at
                )
                store.write(
                    ArtifactName.STORY_BLOCKS, merged, WriteMetadata(fingerprint=model_fp)
                )
                archive_profile(merged, story_rewritten=True)
            except Exception as exc:
                record("story_model_fill", "failed", _elapsed(step_start),
                       detail=str(exc))
                warnings.append(f"步骤 story_model_fill 失败：{exc}（保留 scaffold）")
                archive_profile(scaffold_doc, story_rewritten=scaffold_rewritten)
                return report("partial")
            record("story_model_fill", fill_status, _elapsed(step_start),
                   detail=f"slots={len(merged['slots'])}")
            return report("complete")
        finally:
            lock.release()
