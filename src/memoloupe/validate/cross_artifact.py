"""跨文件一致性校验（docs/02 §5、docs/04 §7.2）。

``validate_output_dir`` 读取 output-dir 下存在的 ``raw/*.json`` 与
``style-profile.json``，先做单文件 schema 校验，再执行跨文件语义检查。
缺失的文件跳过并记 warning；所有发现以 :class:`ValidationIssue`
列表返回，不抛异常。

strict=False 时，complete 状态的镜头级文件未覆盖全部镜头记 warning；
strict=True 时记 error。
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from memoloupe.artifacts.schemas import ArtifactName
from memoloupe.core.atomic_io import read_json
from memoloupe.core.errors import ContractError, EvidenceRefError
from memoloupe.core.evidence_refs import parse_evidence_ref, resolve_json_pointer
from memoloupe.validate.json_contracts import ValidationIssue, validate_file

#: 逻辑名 -> 相对 output-dir 根的路径。
ARTIFACT_PATHS: dict[str, str] = {
    **{name.value: f"raw/{name.value}.json" for name in ArtifactName},
    ArtifactName.STYLE_PROFILE.value: "style-profile.json",
}

_BLOCK_RELATION_REF_RE = re.compile(r"→\s*(B\d{4})")


def _issue(
    artifact: str,
    json_path: str,
    message: str,
    expected: str = "",
    actual: str = "",
    *,
    severity: str = "error",
) -> ValidationIssue:
    return ValidationIssue(
        severity=severity,  # type: ignore[arg-type]
        artifact=artifact,
        json_path=json_path,
        message=message,
        expected=expected,
        actual=actual,
    )


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _shot_list(doc: dict | None) -> list[dict]:
    if not doc:
        return []
    shots = doc.get("shots")
    if not isinstance(shots, list):
        return []
    return [s for s in shots if isinstance(s, dict)]


def _shot_map(shots_doc: dict | None) -> dict[str, dict]:
    """shotID -> shots.json 条目（非法条目跳过）。"""
    result: dict[str, dict] = {}
    for shot in _shot_list(shots_doc):
        sid = shot.get("shotID")
        if isinstance(sid, str):
            result[sid] = shot
    return result


# ---------------------------------------------------------------------------
# 各组检查
# ---------------------------------------------------------------------------


def _check_schema(docs: dict[str, dict], issues: list[ValidationIssue]) -> None:
    for name, data in docs.items():
        issues.extend(validate_file(ArtifactName(name), data))


def _check_revision_consistency(
    docs: dict[str, dict], issues: list[ValidationIssue]
) -> None:
    media = docs.get(ArtifactName.MEDIA.value)
    if not media:
        return
    base = media.get("source", {}).get("revisionID")
    if not isinstance(base, str):
        return
    links = [
        (ArtifactName.FRAME_EVIDENCE.value, "$.request.sourceRevisionID",
         lambda d: d.get("request", {}).get("sourceRevisionID")),
        (ArtifactName.UNIFIED_MEDIA.value, "$.request.sourceRevisionID",
         lambda d: d.get("request", {}).get("sourceRevisionID")),
        (ArtifactName.STYLE_PROFILE.value, "$.source.sourceRevision",
         lambda d: d.get("source", {}).get("sourceRevision")),
    ]
    for artifact, path, getter in links:
        doc = docs.get(artifact)
        if not doc:
            continue
        value = getter(doc)
        if value is None:  # schema 允许 null，表示本次未记录
            continue
        if value != base:
            issues.append(_issue(
                artifact, path,
                "sourceRevision 与 media.json 的 revisionID 不一致",
                expected=base, actual=repr(value),
            ))


def _check_shots_internal(
    docs: dict[str, dict], issues: list[ValidationIssue]
) -> None:
    name = ArtifactName.SHOTS.value
    doc = docs.get(name)
    if not doc:
        return
    shots = _shot_list(doc)
    analysis = doc.get("analysis", {})

    # selectedBoundaryCount 与 boundaries 数量一致。
    selected = analysis.get("selectedBoundaryCount")
    boundaries = doc.get("boundaries")
    if isinstance(selected, int) and isinstance(boundaries, list):
        if selected != len(boundaries):
            issues.append(_issue(
                name, "$.analysis.selectedBoundaryCount",
                "selectedBoundaryCount 与 boundaries 数量不一致",
                expected=str(len(boundaries)), actual=str(selected),
            ))

    sorted_by_final = sorted(
        (s for s in shots if _as_int(s.get("finalStartMs")) is not None),
        key=lambda s: s["finalStartMs"],
    )

    for i, shot in enumerate(shots):
        path = f"$.shots[{i}]"
        seq = _as_int(shot.get("sequenceIndex"))
        if seq is not None and seq != i + 1:
            issues.append(_issue(
                name, f"{path}.sequenceIndex",
                "sequenceIndex 必须从 1 连续递增",
                expected=str(i + 1), actual=str(seq),
            ))
        start = _as_int(shot.get("finalStartMs"))
        end = _as_int(shot.get("finalEndMs"))
        dur = _as_int(shot.get("durationMs"))
        if start is not None and end is not None:
            if end <= start:
                issues.append(_issue(
                    name, path, "final 区间必须满足 finalEndMs > finalStartMs",
                    expected=f"({start}, ...)", actual=str(end),
                ))
            if dur is not None and dur != end - start:
                issues.append(_issue(
                    name, f"{path}.durationMs",
                    "durationMs 必须等于 finalEndMs - finalStartMs",
                    expected=str(end - start), actual=str(dur),
                ))

    # 按 finalStartMs 升序、final 区间无重叠。
    for i in range(len(sorted_by_final) - 1):
        cur, nxt = sorted_by_final[i], sorted_by_final[i + 1]
        cur_end = _as_int(cur.get("finalEndMs"))
        nxt_start = _as_int(nxt.get("finalStartMs"))
        if cur_end is None or nxt_start is None:
            continue
        pair = f"{cur.get('shotID')} -> {nxt.get('shotID')}"
        if nxt_start < cur_end:
            issues.append(_issue(
                name, "$.shots",
                f"final 区间重叠：{pair}",
                expected=f"下一镜头 start >= {cur_end}", actual=str(nxt_start),
            ))

    # 相邻连续性：非 partial 状态下前一 end == 后一 start。
    adjacency_severity = "warning" if _analysis_is_partial(docs) else "error"
    for i in range(len(sorted_by_final) - 1):
        cur, nxt = sorted_by_final[i], sorted_by_final[i + 1]
        cur_end = _as_int(cur.get("finalEndMs"))
        nxt_start = _as_int(nxt.get("finalStartMs"))
        if cur_end is not None and nxt_start is not None and nxt_start > cur_end:
            issues.append(_issue(
                name, "$.shots",
                f"相邻镜头不连续：{cur.get('shotID')} end={cur_end}，"
                f"{nxt.get('shotID')} start={nxt_start}",
                expected=str(cur_end), actual=str(nxt_start),
                severity=adjacency_severity,
            ))

    if shots:
        first_in = shots[0].get("boundaryIn", {})
        if first_in.get("type") != "sourceStart":
            issues.append(_issue(
                name, "$.shots[0].boundaryIn.type",
                "首镜头 boundaryIn 必须为 sourceStart",
                expected="sourceStart", actual=repr(first_in.get("type")),
            ))
        last_out = shots[-1].get("boundaryOut", {})
        if last_out.get("type") != "sourceEnd":
            issues.append(_issue(
                name, f"$.shots[{len(shots) - 1}].boundaryOut.type",
                "末镜头 boundaryOut 必须为 sourceEnd",
                expected="sourceEnd", actual=repr(last_out.get("type")),
            ))

    # analyzedRange 覆盖：首 start == analyzedRange.startMs、末 end == endMs。
    media = docs.get(ArtifactName.MEDIA.value)
    if media and sorted_by_final:
        arange = media.get("source", {}).get("analyzedRange", {})
        a_start = _as_int(arange.get("startMs"))
        a_end = _as_int(arange.get("endMs"))
        first_start = _as_int(sorted_by_final[0].get("finalStartMs"))
        last_end = _as_int(sorted_by_final[-1].get("finalEndMs"))
        if a_start is not None and first_start is not None and first_start != a_start:
            issues.append(_issue(
                name, "$.shots[0].finalStartMs",
                "首镜头起点必须等于 analyzedRange.startMs",
                expected=str(a_start), actual=str(first_start),
            ))
        if a_end is not None and last_end is not None and last_end != a_end:
            issues.append(_issue(
                name, f"$.shots[{len(sorted_by_final) - 1}].finalEndMs",
                "末镜头终点必须等于 analyzedRange.endMs",
                expected=str(a_end), actual=str(last_end),
            ))


def _analysis_is_partial(docs: dict[str, dict]) -> bool:
    media = docs.get(ArtifactName.MEDIA.value)
    if not media:
        return False
    coverage = media.get("source", {}).get("analysisCoverage")
    if not isinstance(coverage, list):
        return False
    return any(
        isinstance(item, dict) and item.get("status") == "partial"
        for item in coverage
    )


def _check_shot_references_and_coverage(
    docs: dict[str, dict], root: Path, strict: bool, issues: list[ValidationIssue]
) -> None:
    shots_doc = docs.get(ArtifactName.SHOTS.value)
    if not shots_doc:
        return
    known = set(_shot_map(shots_doc))
    if not known:
        return

    def clip_ids(doc: dict) -> list[tuple[str, str]]:
        return [
            (f"$.clips[{i}].shotID", c.get("shotID"))
            for i, c in enumerate(doc.get("clips", []))
            if isinstance(c, dict) and isinstance(c.get("shotID"), str)
        ]

    def shot_ids(doc: dict) -> list[tuple[str, str]]:
        return [
            (f"$.shots[{i}].shotID", s.get("shotID"))
            for i, s in enumerate(_shot_list(doc))
            if isinstance(s.get("shotID"), str)
        ]

    def frame_ids(doc: dict) -> list[tuple[str, str]]:
        frames = doc.get("frames")
        if not isinstance(frames, list):
            return []
        return [
            (f"$.frames[{i}].shotID", f.get("shotID"))
            for i, f in enumerate(frames)
            if isinstance(f, dict) and isinstance(f.get("shotID"), str)
        ]

    # (artifact, id 提取器, 是否 complete 判定)
    level_files: list[tuple[str, Any, Any]] = [
        (ArtifactName.AUDIO_CUTS.value, shot_ids,
         lambda d: d.get("status") == "complete"),
        (ArtifactName.FRAME_EVIDENCE.value, frame_ids,
         lambda d: d.get("status") == "complete"),
        (ArtifactName.MUSIC_FLAGS.value, shot_ids,
         lambda d: d.get("status") == "complete"),
        (ArtifactName.UNIFIED_MEDIA.value, clip_ids,
         lambda d: d.get("status") == "complete"),
        (ArtifactName.CAMERA_MOTION.value, shot_ids,
         lambda d: d.get("analysis", {}).get("capabilityStatus") == "complete"),
        (ArtifactName.QUALITY_FLAGS.value, shot_ids,
         lambda d: d.get("status") == "complete"),
        (ArtifactName.AUDIO_ENERGY.value, shot_ids,
         lambda d: d.get("hasAudio") is True),
    ]

    coverage_severity = "error" if strict else "warning"
    for artifact, extractor, is_complete in level_files:
        doc = docs.get(artifact)
        if not doc:
            continue
        covered: set[str] = set()
        for path, sid in extractor(doc):
            if sid not in known:
                issues.append(_issue(
                    artifact, path,
                    "shotID 不存在于 shots.json",
                    expected=f"其中之一 {sorted(known)}", actual=sid,
                ))
            else:
                covered.add(sid)
        if is_complete(doc):
            missing = sorted(known - covered)
            if missing:
                issues.append(_issue(
                    artifact, "$",
                    "complete 状态的镜头级文件未覆盖 shots.json 全部镜头",
                    expected=sorted(known), actual=f"缺失 {missing}",
                    severity=coverage_severity,
                ))


def _check_audio_cuts(docs: dict[str, dict], issues: list[ValidationIssue]) -> None:
    name = ArtifactName.AUDIO_CUTS.value
    doc = docs.get(name)
    if not doc:
        return
    tolerance = _as_int(doc.get("analysis", {}).get("syncToleranceMs"))
    boundary_ids = {
        b.get("audioBoundaryID")
        for b in doc.get("boundaries", [])
        if isinstance(b, dict)
    }
    for i, shot in enumerate(_shot_list(doc)):
        for side in ("boundaryIn", "boundaryOut"):
            boundary = shot.get(side)
            if not isinstance(boundary, dict):
                continue
            path = f"$.shots[{i}].{side}"
            visual = _as_int(boundary.get("visualTimeMs"))
            audio = _as_int(boundary.get("audioTimeMs"))
            offset = _as_int(boundary.get("offsetMs"))
            if audio is not None and offset is not None and visual is not None:
                if offset != audio - visual:
                    issues.append(_issue(
                        name, f"{path}.offsetMs",
                        "offsetMs 必须等于 audioTimeMs - visualTimeMs",
                        expected=str(audio - visual), actual=str(offset),
                    ))
            if boundary.get("classification") == "synchronizedCut":
                if tolerance is not None and offset is not None:
                    if abs(offset) > tolerance:
                        issues.append(_issue(
                            name, f"{path}.offsetMs",
                            "synchronizedCut 的 |offsetMs| 超过 syncToleranceMs",
                            expected=f"|offset| <= {tolerance}", actual=str(offset),
                        ))
            ref = boundary.get("audioBoundaryID")
            if isinstance(ref, str) and ref not in boundary_ids:
                issues.append(_issue(
                    name, f"{path}.audioBoundaryID",
                    "audioBoundaryID 引用不存在于 boundaries",
                    expected=sorted(x for x in boundary_ids if x), actual=ref,
                ))


def _check_frame_evidence(
    docs: dict[str, dict], root: Path, issues: list[ValidationIssue]
) -> None:
    name = ArtifactName.FRAME_EVIDENCE.value
    doc = docs.get(name)
    if not doc:
        return
    shots = _shot_map(docs.get(ArtifactName.SHOTS.value))
    frames = doc.get("frames")
    if not isinstance(frames, list):
        return
    for i, frame in enumerate(frames):
        if not isinstance(frame, dict):
            continue
        path = f"$.frames[{i}]"
        evidence_id = frame.get("evidenceID")
        frame_id = frame.get("frameID")
        if evidence_id is not None and frame_id is not None and evidence_id != frame_id:
            issues.append(_issue(
                name, f"{path}.evidenceID",
                "evidenceID 必须等于 frameID",
                expected=repr(frame_id), actual=repr(evidence_id),
            ))
        sid = frame.get("shotID")
        shot = shots.get(sid) if isinstance(sid, str) else None
        if isinstance(sid, str) and shots and shot is None:
            issues.append(_issue(
                name, f"{path}.shotID", "frame 的 shotID 不存在于 shots.json",
                expected=sorted(shots), actual=sid,
            ))
        time_ms = _as_int(frame.get("timeMs"))
        if shot is not None and time_ms is not None:
            start = _as_int(shot.get("finalStartMs"))
            end = _as_int(shot.get("finalEndMs"))
            if start is not None and end is not None and not (start <= time_ms < end):
                issues.append(_issue(
                    name, f"{path}.timeMs",
                    "帧 timeMs 必须位于所属镜头 final 区间 [startMs, endMs)",
                    expected=f"[{start}, {end})", actual=str(time_ms),
                ))
        file_ref = frame.get("fileRef")
        if isinstance(file_ref, str) and not (root / file_ref).is_file():
            issues.append(_issue(
                name, f"{path}.fileRef", "fileRef 指向的文件不存在",
                expected="存在的相对路径", actual=file_ref,
            ))


def _check_music_flags(docs: dict[str, dict], issues: list[ValidationIssue]) -> None:
    name = ArtifactName.MUSIC_FLAGS.value
    doc = docs.get(name)
    if not doc:
        return
    shots = _shot_list(doc)
    tally = doc.get("stateTally")
    counts = Counter(
        s.get("state") for s in shots if isinstance(s.get("state"), str)
    )
    if isinstance(tally, dict):
        for key in set(tally) | set(counts):
            expected = counts.get(key, 0)
            actual = tally.get(key, 0)
            if actual != expected:
                issues.append(_issue(
                    name, f"$.stateTally.{key}",
                    "stateTally 与 shots 的 state 聚合不一致",
                    expected=str(expected), actual=str(actual),
                ))
    for i, shot in enumerate(shots):
        for field in ("musicOverlapRatio", "silentOverlapRatio"):
            value = shot.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if not 0.0 <= value <= 1.0:
                    issues.append(_issue(
                        name, f"$.shots[{i}].{field}",
                        "overlap ratio 必须在 [0, 1]",
                        expected="0.0..1.0", actual=repr(value),
                    ))


def _check_unified_media(docs: dict[str, dict], issues: list[ValidationIssue]) -> None:
    name = ArtifactName.UNIFIED_MEDIA.value
    doc = docs.get(name)
    if not doc:
        return
    batches = doc.get("batches")
    if isinstance(batches, list):
        for i, batch in enumerate(batches):
            if not isinstance(batch, dict):
                continue
            shot_ids = [s for s in batch.get("shotIDs", []) if isinstance(s, str)]
            response = batch.get("response")
            response_ids: list[str] = []
            if isinstance(response, dict):
                response_ids = [
                    s.get("shotID")
                    for s in response.get("shots", [])
                    if isinstance(s, dict) and isinstance(s.get("shotID"), str)
                ]
            # response 为条件必填：failed 批次允许无 response（docs/07 batch 字段表、
            # docs/02 §4.7 partial 允许成功与永久失败并存）。
            batch_status = batch.get("status")
            if batch_status == "failed" and response is None:
                continue
            if set(shot_ids) != set(response_ids):
                issues.append(_issue(
                    name, f"$.batches[{i}]",
                    "batch.shotIDs 与 batch.response.shots 的 shotID 集合不一致",
                    expected=sorted(shot_ids), actual=sorted(response_ids),
                ))
    statuses = doc.get("shotStatuses")
    if isinstance(statuses, dict):
        clips = doc.get("clips")
        if isinstance(clips, list):
            for i, clip in enumerate(clips):
                if not isinstance(clip, dict):
                    continue
                sid = clip.get("shotID")
                if isinstance(sid, str) and sid not in statuses:
                    issues.append(_issue(
                        name, f"$.clips[{i}].shotID",
                        "shotStatuses 未覆盖该 clip",
                        expected=f"shotStatuses 含 {sid}", actual="缺失",
                    ))
        if doc.get("terminal") is True:
            pending = sorted(k for k, v in statuses.items() if v == "pending")
            if pending:
                issues.append(_issue(
                    name, "$.shotStatuses",
                    "terminal=true 时不得存在 pending 镜头",
                    expected="无 pending", actual=str(pending),
                ))
    if doc.get("status") == "complete":
        for field in ("failedShots", "pendingShots", "permanentFailureShots"):
            value = _as_int(doc.get(field))
            if value is not None and value != 0:
                issues.append(_issue(
                    name, f"$.{field}",
                    "status=complete 时 failed/pending/permanentFailure 计数必须为 0",
                    expected="0", actual=str(value),
                ))


def _check_story_blocks(docs: dict[str, dict], issues: list[ValidationIssue]) -> None:
    name = ArtifactName.STORY_BLOCKS.value
    doc = docs.get(name)
    if not doc:
        return
    shots_doc = docs.get(ArtifactName.SHOTS.value)
    shot_map = _shot_map(shots_doc)
    ordered_ids = [s.get("shotID") for s in _shot_list(shots_doc)]
    order = {sid: i for i, sid in enumerate(ordered_ids) if isinstance(sid, str)}

    blocks = [b for b in doc.get("blocks", []) if isinstance(b, dict)]
    block_ids = {b.get("storyBlockID") for b in blocks}

    membership: Counter[str] = Counter()
    for i, block in enumerate(blocks):
        path = f"$.blocks[{i}]"
        shot_ids = [s for s in block.get("shotIDs", []) if isinstance(s, str)]
        membership.update(shot_ids)
        for j, sid in enumerate(shot_ids):
            if shot_map and sid not in shot_map:
                issues.append(_issue(
                    name, f"{path}.shotIDs[{j}]",
                    "block 引用的 shotID 不存在于 shots.json",
                    expected=sorted(shot_map), actual=sid,
                ))
        known_seqs = [order[s] for s in shot_ids if s in order]
        if len(known_seqs) == len(shot_ids) and known_seqs != sorted(known_seqs):
            issues.append(_issue(
                name, f"{path}.shotIDs",
                "block.shotIDs 必须按镜头顺序升序",
                expected=sorted(shot_ids, key=lambda s: order[s]),
                actual=shot_ids,
            ))
        # block.startMs/endMs 从首尾镜头 final 边界派生。
        if shot_ids and shot_map and all(s in shot_map for s in shot_ids):
            first = shot_map[shot_ids[0]]
            last = shot_map[shot_ids[-1]]
            b_start = _as_int(block.get("startMs"))
            b_end = _as_int(block.get("endMs"))
            f_start = _as_int(first.get("finalStartMs"))
            l_end = _as_int(last.get("finalEndMs"))
            if b_start is not None and f_start is not None and b_start != f_start:
                issues.append(_issue(
                    name, f"{path}.startMs",
                    "block.startMs 必须等于首镜头 finalStartMs",
                    expected=str(f_start), actual=str(b_start),
                ))
            if b_end is not None and l_end is not None and b_end != l_end:
                issues.append(_issue(
                    name, f"{path}.endMs",
                    "block.endMs 必须等于末镜头 finalEndMs",
                    expected=str(l_end), actual=str(b_end),
                ))
        # blockRelation 中的 " → Bxxxx" 引用必须存在且不指自身。
        relation = block.get("blockRelation")
        if isinstance(relation, str):
            for target in _BLOCK_RELATION_REF_RE.findall(relation):
                if target not in block_ids:
                    issues.append(_issue(
                        name, f"{path}.blockRelation",
                        "blockRelation 引用的 block 不存在",
                        expected=sorted(x for x in block_ids if x), actual=target,
                    ))
                elif target == block.get("storyBlockID"):
                    issues.append(_issue(
                        name, f"{path}.blockRelation",
                        "blockRelation 不得指向自身",
                        expected=f"非 {target}", actual=target,
                    ))

    # 每个 shot 恰好属于一个 block。
    if order:
        for sid in order:
            count = membership.get(sid, 0)
            if count == 0:
                issues.append(_issue(
                    name, "$.blocks", "镜头未被任何 block 覆盖",
                    expected="恰好一个 block", actual=sid,
                ))
            elif count > 1:
                issues.append(_issue(
                    name, "$.blocks", "镜头被多个 block 重复覆盖",
                    expected="恰好一个 block", actual=f"{sid} x{count}",
                ))

    # block 连续且不交叉：按首镜头排序后，shotIDs 拼接等于完整镜头序列。
    if order and blocks:
        def _block_key(b: dict) -> int:
            seqs = [order[s] for s in b.get("shotIDs", []) if s in order]
            return min(seqs) if seqs else len(order)

        concat: list[str] = []
        for block in sorted(blocks, key=_block_key):
            seqs = sorted(s for s in block.get("shotIDs", []) if s in order)
            concat.extend(seqs)
        expected_seq = [s for s in ordered_ids if isinstance(s, str)]
        if concat != expected_seq:
            issues.append(_issue(
                name, "$.blocks",
                "block 必须连续且不交叉地覆盖完整镜头序列",
                expected=expected_seq, actual=concat,
            ))

    for i, slot in enumerate(doc.get("slots", [])):
        if not isinstance(slot, dict):
            continue
        for j, bid in enumerate(slot.get("blockIDs", [])):
            if isinstance(bid, str) and bid not in block_ids:
                issues.append(_issue(
                    name, f"$.slots[{i}].blockIDs[{j}]",
                    "slot 引用的 blockID 不存在",
                    expected=sorted(x for x in block_ids if x), actual=bid,
                ))


def _check_style_profile(docs: dict[str, dict], issues: list[ValidationIssue]) -> None:
    name = ArtifactName.STYLE_PROFILE.value
    doc = docs.get(name)
    if not doc:
        return
    story = docs.get(ArtifactName.STORY_BLOCKS.value)
    shot_map = _shot_map(docs.get(ArtifactName.SHOTS.value))
    slots = [
        s for s in doc.get("structure", {}).get("slots", [])
        if isinstance(s, dict)
    ]

    if story:
        story_slot_ids = {
            s.get("slotID")
            for s in story.get("slots", [])
            if isinstance(s, dict) and isinstance(s.get("slotID"), str)
        }
        profile_slot_ids = {
            s.get("slotId") for s in slots if isinstance(s.get("slotId"), str)
        }
        if story_slot_ids != profile_slot_ids:
            issues.append(_issue(
                name, "$.structure.slots",
                "profile slotId 集合与 story-blocks slots 不对齐",
                expected=sorted(story_slot_ids), actual=sorted(profile_slot_ids),
            ))

    shares: list[float] = []
    for i, slot in enumerate(slots):
        path = f"$.structure.slots[{i}]"
        l1 = slot.get("L1", {})
        share = l1.get("durationShare")
        if isinstance(share, (int, float)) and not isinstance(share, bool):
            shares.append(float(share))
            if not 0.0 <= share <= 1.0:
                issues.append(_issue(
                    name, f"{path}.L1.durationShare",
                    "durationShare 必须在 [0, 1]",
                    expected="0.0..1.0", actual=repr(share),
                ))
        l3 = slot.get("L3", {})
        shot_ids = [s for s in l3.get("shotIds", []) if isinstance(s, str)]
        for j, sid in enumerate(shot_ids):
            if shot_map and sid not in shot_map:
                issues.append(_issue(
                    name, f"{path}.L3.shotIds[{j}]",
                    "L3.shotIds 引用的 shotID 不存在于 shots.json",
                    expected=sorted(shot_map), actual=sid,
                ))
        shot_count = _as_int(l3.get("shotCount"))
        if shot_count is not None and shot_count != len(shot_ids):
            issues.append(_issue(
                name, f"{path}.L3.shotCount",
                "shotCount 必须与 L3.shotIds 长度一致",
                expected=str(len(shot_ids)), actual=str(shot_count),
            ))
        avg = l3.get("avgShotSeconds")
        durations = [
            _as_int(shot_map[s].get("durationMs"))
            for s in shot_ids
            if s in shot_map
        ]
        if (
            isinstance(avg, (int, float))
            and not isinstance(avg, bool)
            and durations
            and all(d is not None for d in durations)
        ):
            actual_avg = sum(d for d in durations if d is not None) / len(durations) / 1000
            if abs(actual_avg - float(avg)) > 0.01:
                issues.append(_issue(
                    name, f"{path}.L3.avgShotSeconds",
                    "avgShotSeconds 与镜头时长统计不一致（容差 0.01s）",
                    expected=f"{actual_avg:.4f}", actual=repr(avg),
                ))
    if shares and abs(sum(shares) - 1.0) > 1e-6:
        issues.append(_issue(
            name, "$.structure.slots",
            "全部 slot 的 L1.durationShare 之和必须为 1.0（容差 1e-6）",
            expected="1.0", actual=repr(sum(shares)),
        ))


def _check_evidence_refs(
    docs: dict[str, dict], root: Path, issues: list[ValidationIssue]
) -> None:
    """递归扫描 raw 文件中的 evidenceRefs 字段并验证可解析性。"""
    path_to_doc = {ARTIFACT_PATHS[name]: data for name, data in docs.items()}
    for name, data in docs.items():
        if name == ArtifactName.STYLE_PROFILE.value:
            continue  # 只扫描 raw 文件
        _walk_evidence_refs(name, data, "$", root, path_to_doc, issues)


def _walk_evidence_refs(
    artifact: str,
    node: Any,
    path: str,
    root: Path,
    path_to_doc: dict[str, dict],
    issues: list[ValidationIssue],
) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}"
            if key == "evidenceRefs" and isinstance(value, list):
                for j, ref in enumerate(value):
                    _check_single_ref(
                        artifact, f"{child}[{j}]", ref, root, path_to_doc, issues
                    )
            else:
                _walk_evidence_refs(artifact, value, child, root, path_to_doc, issues)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _walk_evidence_refs(artifact, item, f"{path}[{i}]", root, path_to_doc, issues)


def _check_single_ref(
    artifact: str,
    path: str,
    ref: Any,
    root: Path,
    path_to_doc: dict[str, dict],
    issues: list[ValidationIssue],
) -> None:
    if not isinstance(ref, str):
        issues.append(_issue(
            artifact, path, "evidenceRefs 条目必须是字符串",
            expected="string", actual=repr(ref),
        ))
        return
    try:
        parsed = parse_evidence_ref(ref)
    except EvidenceRefError as exc:
        issues.append(_issue(
            artifact, path, f"evidenceRef 非法：{exc.reason}",
            expected="合法 evidenceRef", actual=ref,
        ))
        return
    if not (root / parsed.file_path).is_file():
        issues.append(_issue(
            artifact, path, "evidenceRef 指向的文件不存在",
            expected="存在的相对路径", actual=ref,
        ))
        return
    if parsed.json_pointer is not None:
        target = path_to_doc.get(parsed.file_path)
        if target is None:
            try:
                target = read_json(root / parsed.file_path)
            except ContractError as exc:
                issues.append(_issue(
                    artifact, path, f"evidenceRef 目标文件无法读取：{exc.actual}",
                    expected="可读 JSON", actual=ref,
                ))
                return
        try:
            resolve_json_pointer(target, parsed.json_pointer)
        except EvidenceRefError as exc:
            issues.append(_issue(
                artifact, path, f"evidenceRef 指针不可解析：{exc.reason}",
                expected="可解析指针", actual=ref,
            ))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def validate_output_dir(root: Path, *, strict: bool = False) -> list[ValidationIssue]:
    """校验一个 output-dir 的全部产物，返回全部 issue（不抛异常）。"""
    root = Path(root)
    issues: list[ValidationIssue] = []
    docs: dict[str, dict] = {}
    for name, rel in ARTIFACT_PATHS.items():
        path = root / rel
        if not path.exists():
            issues.append(_issue(
                name, "$", "artifact 文件缺失，跳过相关检查",
                expected=rel, actual="missing", severity="warning",
            ))
            continue
        try:
            docs[name] = read_json(path)
        except ContractError as exc:
            issues.append(_issue(
                name, exc.json_path, exc.message or "JSON 读取失败",
                expected=exc.expected, actual=exc.actual,
            ))

    _check_schema(docs, issues)
    _check_revision_consistency(docs, issues)
    _check_shots_internal(docs, issues)
    _check_shot_references_and_coverage(docs, root, strict, issues)
    _check_audio_cuts(docs, issues)
    _check_frame_evidence(docs, root, issues)
    _check_music_flags(docs, issues)
    _check_unified_media(docs, issues)
    _check_story_blocks(docs, issues)
    _check_style_profile(docs, issues)
    _check_evidence_refs(docs, root, issues)
    return issues
