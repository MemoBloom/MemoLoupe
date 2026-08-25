"""analysis.profile_pipeline — Phase 3 风格档案编排。

锚点：docs/03 §4、roadmap 04-02/04-03。

流程（先确定性聚合，再模型蒸馏；docs/03 §4）：

1. ``load_inputs``：media/shots/story-blocks 必需；asr/audio-cuts/music-flags/
   camera-motion/unified-media 缺失显式降级（对应聚合字段为空/保守值）；
2. ``profile_aggregate``：纯函数聚合（:mod:`memoloupe.analysis.profile_aggregate`，
   不调用模型）→ 原子写根目录 ``style-profile.json``（``distillStatus=skipped``），
   指纹复用或重建；
3. ``profile_distill``：配置文本模型时请求蒸馏（prompt 只含结构化文本），
   解析校验（:func:`parse_profile_distill`）→ 白名单合并（确定性字段模型无权
   覆盖）→ 重写 ``distillStatus=complete``；每次成功请求后 checkpoint，重跑
   指纹命中不重发请求；模型不可用/不合规时保留 aggregate 文件
   （``distillStatus=skipped`` 为未成功蒸馏的合法状态），report partial。
"""

from __future__ import annotations

import copy
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memoloupe.analysis.media_orchestrator import _strip_single_fence
from memoloupe.analysis.profile_aggregate import (
    PROFILE_AGGREGATE_VERSION,
    build_profile_aggregate,
)
from memoloupe.analysis.profile_prompts import (
    DETERMINISTIC_SLOT_FIELDS,
    NARRATIVE_FUNCTIONS,
    PROFILE_PROMPT_VERSION,
    build_profile_distill_prompt,
)
from memoloupe.analysis.vocabulary import load_vocabulary
from memoloupe.analysis.shot_pipeline import (  # noqa: F401
    PipelineReport,
    StepRecord,
    _Lock as _OutputLock,
)
from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
from memoloupe.artifacts.store import ArtifactStore, WriteMetadata
from memoloupe.core.atomic_io import read_json, write_json_atomic
from memoloupe.core.errors import MemoLoupeError
from memoloupe.core.hashing import content_revision_id, fingerprint
from memoloupe.core.logging import get_logger, log_step
from memoloupe.services.text_model import TextModelRequest

PROFILE_DISTILL_VERSION = "profile-distill.v1"

_SLOT_ID_RE = re.compile(r"^S\d{3}$")
_BLOCK_ID_RE = re.compile(r"^B\d{4}$")
_SHOT_ID_RE = re.compile(r"^SH\d{4}$")


@dataclass(frozen=True)
class ProfileBuildRequest:
    """风格档案构建请求。``text_service`` 为 None 时只产出确定性聚合。"""

    output_dir: Path
    text_service: Any = None  # TextModelService；None 时 distillStatus=skipped
    force: frozenset[str] = frozenset()
    no_cache: bool = False


class InvalidProfileResponse(Exception):
    """模型蒸馏响应不合规：整体拒绝，保留确定性聚合。"""


# ---------------------------------------------------------------------------
# 蒸馏响应解析与白名单合并（04-02）
# ---------------------------------------------------------------------------


def _nullable_text(value: Any) -> str | None:
    """可选文本字段：str 去空白；空/缺失/非 str → None（不编造）。"""
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _text(value: Any) -> str:
    """必填文本字段：非 str → 空串；str 原样保留（含空）。"""
    return value if isinstance(value, str) else ""


def _narrative_function(value: Any) -> str | None:
    """narrativeFunction：受控词表内返回；缺失/表外 → None（保守）。"""
    if isinstance(value, str) and value.strip() in NARRATIVE_FUNCTIONS:
        return value.strip()
    return None


def _parse_slot_fields(raw: dict, slot_id: str) -> dict:
    """单个 slot 的模型补充字段；白名单字段出现在任意层级都拒绝。"""
    for key in DETERMINISTIC_SLOT_FIELDS:
        if key in raw or key in raw.get("L1", {}) or key in raw.get("L3", {}):
            raise InvalidProfileResponse(
                f"{slot_id} 试图覆盖确定性字段 {key}"
                "（模型无权修改统计/ID/时长/分布）"
            )
    l1_raw = raw.get("L1")
    l2_raw = raw.get("L2")
    if not isinstance(l1_raw, dict) or not isinstance(l2_raw, dict):
        raise InvalidProfileResponse(f"{slot_id} 缺 L1/L2 对象")
    return {
        "L1": {
            "functionalTitle": _nullable_text(l1_raw.get("functionalTitle")),
            "narrativeFunction": _narrative_function(l1_raw.get("narrativeFunction")),
            "intendedReaction": _nullable_text(l1_raw.get("intendedReaction")),
        },
        "L2": {
            "carriage": _nullable_text(l2_raw.get("carriage")),
            "pattern": _nullable_text(l2_raw.get("pattern")),
            "referenceContent": _text(l2_raw.get("referenceContent")),
        },
    }


def _parse_layered_role(
    value: Any,
    name: str,
    expected_slots: set[str],
    block_ids: set[str],
    available_shot_ids: set[str],
    block_to_slots: dict[str, set[str]],
    block_to_shots: dict[str, set[str]],
) -> dict | None:
    """hook/payoff：null 或合法 layeredRole；引用必须闭合。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidProfileResponse(f"{name} 必须是对象或 null")
    l1 = value.get("L1")
    l2 = value.get("L2")
    l3 = value.get("L3")
    if not isinstance(l1, dict) or not isinstance(l2, dict) or not isinstance(l3, dict):
        raise InvalidProfileResponse(f"{name} 缺 L1/L2/L3 对象")
    at_seconds = l1.get("atSeconds")
    if not isinstance(at_seconds, (int, float)) or isinstance(at_seconds, bool):
        raise InvalidProfileResponse(f"{name}.L1.atSeconds 必须是数字")
    slot_id = l1.get("slotId")
    block_id = l1.get("blockId")
    if not isinstance(slot_id, str) or not _SLOT_ID_RE.match(slot_id):
        raise InvalidProfileResponse(f"{name}.L1.slotId 非法：{slot_id!r}")
    if not isinstance(block_id, str) or not _BLOCK_ID_RE.match(block_id):
        raise InvalidProfileResponse(f"{name}.L1.blockId 非法：{block_id!r}")
    if slot_id not in expected_slots:
        raise InvalidProfileResponse(f"{name}.L1.slotId 不存在：{slot_id}")
    if block_id not in block_ids:
        raise InvalidProfileResponse(f"{name}.L1.blockId 不存在：{block_id}")
    if block_to_slots and slot_id not in block_to_slots.get(block_id, set()):
        raise InvalidProfileResponse(
            f"{name}.L1.blockId {block_id} 不属于 slotId {slot_id}"
        )
    shot_ids = l3.get("shotIds")
    if not isinstance(shot_ids, list) or not shot_ids:
        raise InvalidProfileResponse(f"{name}.L3.shotIds 必须是非空数组")
    normalized_shot_ids: list[str] = []
    for index, shot_id in enumerate(shot_ids):
        if not isinstance(shot_id, str) or not _SHOT_ID_RE.match(shot_id):
            raise InvalidProfileResponse(
                f"{name}.L3.shotIds[{index}] 非法：{shot_id!r}"
            )
        if available_shot_ids and shot_id not in available_shot_ids:
            raise InvalidProfileResponse(
                f"{name}.L3.shotIds[{index}] 不存在：{shot_id}"
            )
        if block_to_shots and shot_id not in block_to_shots.get(block_id, set()):
            raise InvalidProfileResponse(
                f"{name}.L3.shotIds[{index}] 不属于 blockId {block_id}"
            )
        normalized_shot_ids.append(shot_id)
    form = l2.get("form")
    reference = l2.get("referenceContent")
    if not isinstance(form, str) or not form.strip():
        raise InvalidProfileResponse(f"{name}.L2.form 必须是非空字符串")
    if not isinstance(reference, str) or not reference.strip():
        raise InvalidProfileResponse(f"{name}.L2.referenceContent 必须是非空字符串")
    return {
        "L1": {
            "atSeconds": float(at_seconds),
            "slotId": slot_id,
            "blockId": block_id,
        },
        "L2": {"form": form, "referenceContent": reference},
        "L3": {"shotIds": normalized_shot_ids},
    }


def _parse_structure_requirements(
    value: Any, expected_slots: set[str]
) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidProfileResponse("structureRequirements 必须是数组")
    result: list[dict] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise InvalidProfileResponse(f"structureRequirements[{index}] 必须是对象")
        slot_id = item.get("slotId")
        if not isinstance(slot_id, str) or slot_id not in expected_slots:
            raise InvalidProfileResponse(
                f"structureRequirements[{index}].slotId 不存在：{slot_id!r}"
            )
        for key in ("requirementType", "description", "minEvidence"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise InvalidProfileResponse(
                    f"structureRequirements[{index}].{key} 必须是非空字符串"
                )
        result.append(
            {
                "slotId": slot_id,
                "requirementType": item["requirementType"],
                "description": item["description"],
                "minEvidence": item["minEvidence"],
            }
        )
    return result


def _parse_adoption_hints(value: Any) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidProfileResponse("adoptionHints 必须是对象或 null")
    for key in ("strengths", "cautions"):
        items = value.get(key)
        if not isinstance(items, list) or not all(
            isinstance(s, str) for s in items
        ):
            raise InvalidProfileResponse(f"adoptionHints.{key} 必须是字符串数组")
    suggested = value.get("suggestedDefault")
    if not isinstance(suggested, str) or not suggested.strip():
        raise InvalidProfileResponse("adoptionHints.suggestedDefault 必须是非空字符串")
    return {
        "strengths": list(value["strengths"]),
        "cautions": list(value["cautions"]),
        "suggestedDefault": suggested,
    }


def _parse_discussion_items(value: Any) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidProfileResponse("discussionItems 必须是数组")
    result: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise InvalidProfileResponse(f"discussionItems[{index}] 必须是对象")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise InvalidProfileResponse(f"discussionItems[{index}].id 必须是非空字符串")
        if item_id in seen:
            raise InvalidProfileResponse(f"discussionItems[{index}] id 重复：{item_id}")
        seen.add(item_id)
        options = item.get("options")
        if not isinstance(options, list) or not options:
            raise InvalidProfileResponse(f"discussionItems[{index}].options 必须是非空数组")
        if not all(
            isinstance(o, dict) and isinstance(o.get("id"), str)
            and isinstance(o.get("label"), str) and o["label"].strip()
            for o in options
        ):
            raise InvalidProfileResponse(
                f"discussionItems[{index}].options 每项必须含 id 与 label"
            )
        for key in ("layer", "category", "question", "impactLevel", "defaultIfUnanswered"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise InvalidProfileResponse(
                    f"discussionItems[{index}].{key} 必须是非空字符串"
                )
        result.append(
            {
                "id": item_id,
                "layer": item["layer"],
                "category": item["category"],
                "question": item["question"],
                "options": [{"id": o["id"], "label": o["label"]} for o in options],
                "impactLevel": item["impactLevel"],
                "defaultIfUnanswered": item["defaultIfUnanswered"],
            }
        )
    return result


def parse_profile_distill(text: str, aggregate: dict, story: dict) -> dict:
    """解析并校验模型蒸馏响应，返回可合并的白名单结果。

    任何不合规抛 :class:`InvalidProfileResponse`：非法 JSON、slot 集合不闭合、
    试图覆盖确定性字段、hook/payoff 引用不闭合、蒸馏字段结构非法等。
    """
    stripped = _strip_single_fence(text)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise InvalidProfileResponse(f"非法 JSON: {exc.msg}") from None
    if not isinstance(data, dict):
        raise InvalidProfileResponse(f"响应不是 JSON 对象: {type(data).__name__}")

    expected_slots = {s["slotId"] for s in aggregate["structure"]["slots"]}
    block_ids = {
        str(b["storyBlockID"])
        for b in (story.get("blocks") if isinstance(story, dict) else []) or []
        if isinstance(b, dict) and isinstance(b.get("storyBlockID"), str)
    }
    block_to_shots = {
        str(b["storyBlockID"]): {
            str(sid) for sid in b.get("shotIDs", []) if isinstance(sid, str)
        }
        for b in (story.get("blocks") if isinstance(story, dict) else []) or []
        if isinstance(b, dict) and isinstance(b.get("storyBlockID"), str)
    }
    available_shot_ids = {
        str(sid)
        for slot in aggregate["structure"]["slots"]
        for sid in slot.get("L3", {}).get("shotIds", [])
        if isinstance(sid, str)
    }
    block_to_slots: dict[str, set[str]] = {}
    for slot in (story.get("slots") if isinstance(story, dict) else []) or []:
        if not isinstance(slot, dict) or not isinstance(slot.get("slotID"), str):
            continue
        slot_id = slot["slotID"]
        for bid in slot.get("blockIDs", []):
            if isinstance(bid, str):
                block_to_slots.setdefault(bid, set()).add(slot_id)

    raw_slots = data.get("slots")
    if not isinstance(raw_slots, list):
        raise InvalidProfileResponse("slots 缺失或不是数组")
    if {str(s.get("slotId")) for s in raw_slots if isinstance(s, dict)} != expected_slots:
        raise InvalidProfileResponse(
            f"slot 集合不一致: 期望 {sorted(expected_slots)}"
        )
    slots: dict[str, dict] = {}
    for raw in raw_slots:
        if not isinstance(raw, dict):
            raise InvalidProfileResponse("slots 含非对象条目")
        slot_id = str(raw["slotId"])
        slots[slot_id] = _parse_slot_fields(raw, slot_id)

    hook = _parse_layered_role(
        data.get("hook"),
        "hook",
        expected_slots,
        block_ids,
        available_shot_ids,
        block_to_slots,
        block_to_shots,
    )
    payoff = _parse_layered_role(
        data.get("payoff"),
        "payoff",
        expected_slots,
        block_ids,
        available_shot_ids,
        block_to_slots,
        block_to_shots,
    )

    return {
        "slots": slots,
        "hook": hook,
        "payoff": payoff,
        "structureRequirements": _parse_structure_requirements(
            data.get("structureRequirements"), expected_slots
        ),
        "adoptionHints": _parse_adoption_hints(data.get("adoptionHints")),
        "discussionItems": _parse_discussion_items(data.get("discussionItems")),
    }


def merge_profile_distill(aggregate: dict, result: dict, *, generated_at: str) -> dict:
    """把模型蒸馏结果合并到确定性聚合上（白名单合并）。

    确定性字段（L1.types/durationShare/rangeSeconds/minBlocks、L3、
    pacing/style/asrTextStats/source）结构上只从 aggregate 复制。
    """
    merged = copy.deepcopy(aggregate)
    for slot in merged["structure"]["slots"]:
        model = result["slots"][slot["slotId"]]
        slot["L1"].update({k: v for k, v in model["L1"].items() if v is not None})
        slot["L2"].update(model["L2"])
    merged["structure"]["hook"] = result["hook"]
    merged["structure"]["payoff"] = result["payoff"]
    merged["structureRequirements"] = result["structureRequirements"]
    merged["adoptionHints"] = result["adoptionHints"]
    merged["discussionItems"] = result["discussionItems"]
    merged["distillStatus"] = "complete"
    merged["createdAt"] = generated_at
    return merged


# ---------------------------------------------------------------------------
# 蒸馏 checkpoint（与最终 artifact 分离，docs/03 §5.2）
# ---------------------------------------------------------------------------


def _checkpoint_path(store: ArtifactStore) -> Path:
    return store.root / "checkpoints" / "style-profile-distill.json"


def _write_checkpoint(
    store: ArtifactStore, fp: str, generated_at: str, result: dict
) -> None:
    write_json_atomic(
        _checkpoint_path(store),
        {
            "version": PROFILE_DISTILL_VERSION,
            "fingerprint": fp,
            "generatedAt": generated_at,
            "result": result,
        },
    )


def _load_checkpoint(store: ArtifactStore, fp: str) -> tuple[dict, str] | None:
    try:
        data = read_json(_checkpoint_path(store))
    except (MemoLoupeError, OSError):
        return None
    if (
        data.get("version") != PROFILE_DISTILL_VERSION
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
    path = store.path(name)
    return content_revision_id(path) if path.exists() else "none"


class ProfileBuildPipeline:
    """Phase 3 风格档案编排器（确定性聚合 + 可选模型蒸馏）。"""

    def run(self, request: ProfileBuildRequest) -> PipelineReport:
        started = time.monotonic()
        run_id = uuid.uuid4().hex[:8]
        logger = get_logger(
            "memoloupe.analysis.profile_pipeline", run_id=run_id, phase="profile"
        )
        out_dir = Path(request.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        store = ArtifactStore(out_dir)
        steps: list[StepRecord] = []
        warnings: list[str] = []

        def record(name: str, status: str, elapsed_ms: int, detail: str | None = None) -> None:
            log_step(logger, name, status, elapsed_ms, **({"detail": detail} if detail else {}))
            steps.append(StepRecord(name=name, status=status, elapsed_ms=elapsed_ms, detail=detail))

        def report(status: str) -> PipelineReport:
            return PipelineReport(
                phase="profile",
                status=status,
                steps=steps,
                warnings=warnings,
                artifacts=(
                    ["style-profile.json"]
                    if store.exists(ArtifactName.STYLE_PROFILE)
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
            # 1. load_inputs：media/shots/story-blocks 必需。
            step_start = time.monotonic()
            try:
                media = store.read(ArtifactName.MEDIA)
                shots_doc = store.read(ArtifactName.SHOTS)
                story = store.read(ArtifactName.STORY_BLOCKS)
            except MemoLoupeError as exc:
                record("load_inputs", "failed", _elapsed(step_start), detail=str(exc))
                warnings.append(f"风格档案输入不可用：{exc}")
                return report("failed")
            raws: dict[str, dict | None] = {
                "media": media,
                "shots": shots_doc,
                "story-blocks": story,
            }
            for name in (
                ArtifactName.ASR, ArtifactName.AUDIO_CUTS,
                ArtifactName.MUSIC_FLAGS, ArtifactName.CAMERA_MOTION,
                ArtifactName.UNIFIED_MEDIA,
            ):
                try:
                    raws[name.value] = store.read(name)
                except MemoLoupeError:
                    raws[name.value] = None
                    warnings.append(f"{name.value}.json 缺失或非法，按降级处理")
            record("load_inputs", "complete", _elapsed(step_start),
                   detail=f"slots={len(story.get('slots', []))}")

            # 2. profile_aggregate：指纹复用或重建。
            step_start = time.monotonic()
            vocabulary = load_vocabulary()
            agg_fp = fingerprint(
                {
                    "artifact": "style-profile",
                    "media": _file_revision(store, ArtifactName.MEDIA),
                    "shots": _file_revision(store, ArtifactName.SHOTS),
                    "storyBlocks": _file_revision(store, ArtifactName.STORY_BLOCKS),
                    "asr": _file_revision(store, ArtifactName.ASR),
                    "audioCuts": _file_revision(store, ArtifactName.AUDIO_CUTS),
                    "musicFlags": _file_revision(store, ArtifactName.MUSIC_FLAGS),
                    "cameraMotion": _file_revision(store, ArtifactName.CAMERA_MOTION),
                    "unifiedMedia": _file_revision(store, ArtifactName.UNIFIED_MEDIA),
                    "vocabVersion": vocabulary.version,
                    "version": PROFILE_AGGREGATE_VERSION,
                }
            )
            distill_fp = fingerprint(
                {
                    "artifact": "style-profile-distill",
                    "aggregate": agg_fp,
                    "promptVersion": PROFILE_PROMPT_VERSION,
                    "service": "injected" if request.text_service is not None else "none",
                    "version": PROFILE_DISTILL_VERSION,
                }
            )
            agg_cacheable = not request.no_cache and "profile_aggregate" not in request.force
            distill_cacheable = not request.no_cache and "profile_distill" not in request.force
            if (
                request.text_service is not None
                and agg_cacheable
                and distill_cacheable
                and store.is_reusable(ArtifactName.STYLE_PROFILE, distill_fp)
            ):
                record("profile_aggregate", "reused", _elapsed(step_start),
                       detail=f"fingerprint={agg_fp}")
                record("profile_distill", "reused", 0, detail=f"fingerprint={distill_fp}")
                return report("complete")

            try:
                aggregate = build_profile_aggregate(raws, vocabulary=vocabulary)
                if agg_cacheable and store.is_reusable(ArtifactName.STYLE_PROFILE, agg_fp):
                    record("profile_aggregate", "reused", _elapsed(step_start),
                           detail=f"fingerprint={agg_fp}")
                else:
                    validate_artifact(ArtifactName.STYLE_PROFILE, aggregate)
                    store.write(
                        ArtifactName.STYLE_PROFILE,
                        aggregate,
                        WriteMetadata(fingerprint=agg_fp),
                    )
                    record("profile_aggregate", "complete", _elapsed(step_start),
                           detail=f"slots={len(aggregate['structure']['slots'])}")
            except Exception as exc:
                record("profile_aggregate", "failed", _elapsed(step_start),
                       detail=str(exc))
                warnings.append(f"步骤 profile_aggregate 失败：{exc}")
                return report("failed")
            if request.text_service is None:
                return report("complete")

            # 3. profile_distill：checkpoint 复用或请求模型；失败保留 aggregate
            #    并把 distillStatus 置 unavailable（失败可见）。
            step_start = time.monotonic()
            try:
                cached = _load_checkpoint(store, distill_fp) if distill_cacheable else None
                if cached is not None:
                    result, generated_at = cached
                    distill_status = "reused"
                else:
                    prompt = build_profile_distill_prompt(aggregate, story)
                    text = request.text_service.generate(
                        TextModelRequest(task="profile-distill", prompt=prompt)
                    )
                    result = parse_profile_distill(text, aggregate, story)
                    generated_at = datetime.now(UTC).isoformat(
                        timespec="milliseconds"
                    ).replace("+00:00", "Z")
                    _write_checkpoint(store, distill_fp, generated_at, result)
                    distill_status = "complete"
                merged = merge_profile_distill(aggregate, result, generated_at=generated_at)
                validate_artifact(ArtifactName.STYLE_PROFILE, merged)
                store.write(
                    ArtifactName.STYLE_PROFILE,
                    merged,
                    WriteMetadata(fingerprint=distill_fp),
                )
            except Exception as exc:
                record("profile_distill", "failed", _elapsed(step_start),
                       detail=str(exc))
                warnings.append(f"步骤 profile_distill 失败：{exc}（保留确定性聚合）")
                # 失败可见：保留 aggregate 文件（distillStatus=skipped 即未成功
                # 蒸馏的合法状态），report partial 呈现失败；不重写文件以免
                # 污染聚合指纹的复用判定。
                return report("partial")
            record("profile_distill", distill_status, _elapsed(step_start),
                   detail=f"slots={len(result['slots'])}")
            return report("complete")
        finally:
            lock.release()
