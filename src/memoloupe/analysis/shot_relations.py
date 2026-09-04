"""相邻镜头关系分析（Phase 06-03/04，schemas/shot-relations.json）。

分层红线（docs 计划 §3）：

- ``metrics``：确定性指标，只从既有 artifact 派生或对边界帧实测；
  数据不足落 unknown/unavailable，绝不从缺失数组推断"没有变化"；
- ``semantic``：可选模型语义（MiMo 文本模型），失败/未配置显式 unknown；
- 模型不得改变 pair 集合、镜头边界或指标数值；
- pair 数量严格为 ``max(shotCount - 1, 0)``，顺序与 ``shots.json`` 一致。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from memoloupe.analysis.shot_relation_prompts import (
    PairSemanticParseError,
    parse_pair_semantics,
)
from memoloupe.media.proc import ProcessError
from memoloupe.media.review_timeline import REVIEW_TIMELINE_VERSION
from memoloupe.media.transition_evidence import (
    TRANSITION_EVIDENCE_VERSION,
    boundary_frame_times_from_pts,
    extract_transition_evidence,
    measure_frame_luma,
)
from memoloupe.services.shot_relation_model import (
    ShotRelationSemanticsService,
)

SHOT_RELATIONS_VERSION = "shot-relations.v1"

#: 音频切点与镜头边界视为"对齐"的容差（与 audio-cuts syncTolerance 同量级）。
AUDIO_CUT_ALIGN_TOLERANCE_MS = 100
#: 语音停顿超过该值提示人工复核（CALIBRATION）。
SPEECH_GAP_REVIEW_MS = 800
#: 亮度差超过该值提示人工复核（CALIBRATION）。
LUMA_DELTA_REVIEW = 0.40
#: 亮度几乎无变化提示人工核对软切/误切（CALIBRATION）。
LUMA_DELTA_SUSPECT_CUT = 0.08

_LUMA_REVIEW = f"边界两侧亮度差 ≥ {LUMA_DELTA_REVIEW}，建议人工核对切点"
_LUMA_SUSPECT = (
    f"镜头切换处画面亮度差 < {LUMA_DELTA_SUSPECT_CUT}，"
    "建议人工核对是否软切/同镜头误切"
)
_SPEECH_GAP_REVIEW = f"切点处语音停顿 ≥ {SPEECH_GAP_REVIEW_MS}ms"


def _metric(
    value: object,
    *,
    status: str,
    evidence_refs: tuple[str, ...] = (),
    unit: str | None = None,
) -> dict:
    metric: dict = {"status": status, "evidenceRefs": list(evidence_refs)}
    if status != "unavailable":
        metric["value"] = value
    if unit:
        metric["unit"] = unit
    return metric


def _index_by_shot(doc: dict | None) -> dict[str, tuple[int, dict]]:
    """``shotID → (数组下标, 条目)``；证据引用与取值共用。"""
    if not isinstance(doc, dict) or not isinstance(doc.get("shots"), list):
        return {}
    return {
        str(s.get("shotID")): (i, s)
        for i, s in enumerate(doc["shots"])
        if isinstance(s, dict) and s.get("shotID")
    }


def _asr_segments(asr_doc: dict | None) -> list[dict]:
    if not isinstance(asr_doc, dict):
        return []
    segments = (asr_doc.get("transcript") or {}).get("segments")
    if not isinstance(segments, list):
        return []
    return [s for s in segments if isinstance(s, dict)]


def _speech_metrics(
    segments: list[dict], *, boundary_ms: int
) -> tuple[dict, dict, list[str]]:
    """跨切点语音指标：``(speechGapMs, speechSpansBoundary, evidenceRefs)``。

    - gap：boundary 前最后一个 segment 的 endMs 到 boundary 后第一个
      segment 的 startMs 的间隔；存在跨切 segment 时为 0；
    - refs 指向参与计算的 asr.json segment（源证据，不指向自身）。
    """
    prev_end: int | None = None
    prev_index: int | None = None
    next_start: int | None = None
    next_index: int | None = None
    spans = False
    span_index: int | None = None
    for i, seg in enumerate(segments):
        start = seg.get("startMs")
        end = seg.get("endMs")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        if start < boundary_ms < end:
            spans = True
            span_index = i
        if end <= boundary_ms and (prev_end is None or end > prev_end):
            prev_end = end
            prev_index = i
        if start >= boundary_ms and (next_start is None or start < next_start):
            next_start = start
            next_index = i
    refs: list[str] = []
    for i in (prev_index, span_index, next_index):
        if i is not None:
            refs.append(f"raw/asr.json#transcript.segments[{i}]")
    gap: int | None
    if spans:
        gap = 0
    elif prev_end is not None and next_start is not None:
        gap = max(int(next_start) - int(prev_end), 0)
    else:
        gap = None
    gap_metric = (
        _metric(gap, status="value", evidence_refs=tuple(refs), unit="ms")
        if gap is not None
        else _metric(None, status="unknown")
    )
    spans_metric = (
        _metric(spans, status="value", evidence_refs=tuple(refs))
        if segments
        else _metric(None, status="unknown")
    )
    return gap_metric, spans_metric, refs


def _model_payload(
    left: dict,
    right: dict,
    boundary_ms: int,
    metrics: dict,
    segments: list[dict],
    frame_notes: dict[str, str | None],
) -> dict:
    """白名单模型输入：结构化摘要 + 确定性指标 + 边界附近对白。"""

    def shot_payload(shot: dict) -> dict:
        return {
            "shotID": shot.get("shotID"),
            "startMs": shot.get("finalStartMs"),
            "endMs": shot.get("finalEndMs"),
            "durationMs": shot.get("durationMs"),
            "boundaryTypeIn": (shot.get("boundaryIn") or {}).get("type"),
            "boundaryTypeOut": (shot.get("boundaryOut") or {}).get("type"),
        }

    nearby_text = [
        str(seg.get("text", "")).strip()
        for seg in segments
        if isinstance(seg.get("endMs"), int)
        and isinstance(seg.get("startMs"), int)
        and seg["endMs"] > boundary_ms - 3000
        and seg["startMs"] < boundary_ms + 3000
    ]
    nearby_text = [t for t in nearby_text if t][:4]
    payload: dict = {
        "boundaryMs": boundary_ms,
        "left": shot_payload(left),
        "right": shot_payload(right),
        "deterministicMetrics": {
            k: {"value": v.get("value"), "status": v.get("status")}
            for k, v in metrics.items()
        },
        "nearbySpeechText": nearby_text,
    }
    if frame_notes.get("left"):
        payload["leftExitNote"] = frame_notes["left"]
    if frame_notes.get("right"):
        payload["rightEntryNote"] = frame_notes["right"]
    return payload


def build_shot_relations(
    source: Path,
    shots: list[dict],
    config: dict,
    out_dir: Path,
    *,
    pool,
    model_service: ShotRelationSemanticsService | None,
    source_revision_id: str,
    energy_doc: dict | None,
    music_doc: dict | None,
    camera_doc: dict | None,
    asr_doc: dict | None,
    audio_cuts_doc: dict | None,
    review_timeline_doc: dict | None = None,
) -> dict:
    """生成 ``raw/shot-relations.json``（确定性层 + 可选语义层）。"""
    source = Path(source)
    out_dir = Path(out_dir)
    ffmpeg = str(config["ffmpeg"]["ffmpegPath"])
    timeout = float(config["ffmpeg"]["frameTimeoutSec"])

    energy = _index_by_shot(energy_doc)
    music = _index_by_shot(music_doc)
    camera = _index_by_shot(camera_doc)
    segments = _asr_segments(asr_doc)
    audio_boundaries: list[int] = []
    if isinstance(audio_cuts_doc, dict) and isinstance(audio_cuts_doc.get("boundaries"), list):
        audio_boundaries = [
            int(b["timeMs"])
            for b in audio_cuts_doc["boundaries"]
            if isinstance(b, dict) and isinstance(b.get("timeMs"), int)
        ]

    # 1. 边界证据帧（失败不中断，写显式状态） --------------------------------
    # 用真实 PTS 索引定位边界帧：endMs-1 与 startMs 在低帧率下可能落入同一
    # 展示帧，导致 lumaDelta 恒为 0（索引不可用时退化为估算并标记降级）。
    pts_ms = None
    if isinstance(review_timeline_doc, dict):
        frames = review_timeline_doc.get("videoFrames") or {}
        if frames.get("timingMode") == "pts-index" and isinstance(frames.get("ptsMs"), list):
            pts_ms = frames["ptsMs"]
    frame_times: dict[str, dict[str, int]] = {}
    for _i in range(1, len(shots)):
        _l, _r = shots[_i - 1], shots[_i]
        _pair = f"{_l['shotID']}--{_r['shotID']}"
        _l_t, _r_t = boundary_frame_times_from_pts(
            pts_ms,
            boundary_ms=int(_l["finalEndMs"]),
            left_end_ms=int(_l["finalEndMs"]),
            right_start_ms=int(_r["finalStartMs"]),
        )
        frame_times[_pair] = {"left-exit": _l_t, "right-entry": _r_t}
    boundary_evidence = extract_transition_evidence(
        source, shots, config, out_dir, pool=pool, frame_times=frame_times
    )

    relations: list[dict] = []
    warnings: list[str] = []
    semantic_failed = 0
    semantic_payloads: list[tuple[int, dict]] = []

    for index in range(1, len(shots)):
        left = shots[index - 1]
        right = shots[index]
        left_id = str(left["shotID"])
        right_id = str(right["shotID"])
        pair_id = f"{left_id}--{right_id}"
        boundary_ms = int(left["finalEndMs"])

        metrics: dict[str, dict] = {}
        review_reasons: list[str] = []

        # 1) lumaDelta：边界帧实测（证据 = 边界帧文件） ------------------------
        ev = boundary_evidence.get(pair_id, {})
        left_frame = ev.get("left-exit", {})
        right_frame = ev.get("right-entry", {})
        luma_refs: list[str] = []
        lumas: list[float] = []
        for side, frame in (("left-exit", left_frame), ("right-entry", right_frame)):
            if frame.get("status") == "complete" and frame.get("fileRef"):
                luma_refs.append(str(frame["fileRef"]))
                try:
                    lumas.append(
                        measure_frame_luma(
                            out_dir / str(frame["fileRef"]),
                            ffmpeg_path=ffmpeg,
                            timeout_sec=timeout,
                            pool=pool,
                        )
                    )
                except (ProcessError, ValueError) as exc:
                    warnings.append(f"{pair_id} {side} 亮度测量失败：{exc}")
        if len(lumas) == 2:
            delta = round(lumas[1] - lumas[0], 3)
            metrics["lumaDelta"] = _metric(
                delta, status="value", evidence_refs=tuple(luma_refs)
            )
            if abs(delta) >= LUMA_DELTA_REVIEW:
                review_reasons.append(_LUMA_REVIEW)
            elif abs(delta) < LUMA_DELTA_SUSPECT_CUT:
                review_reasons.append(_LUMA_SUSPECT)
        else:
            metrics["lumaDelta"] = _metric(
                None, status="unavailable", evidence_refs=tuple(luma_refs)
            )
            review_reasons.append("边界证据帧不可用，亮度差未测量")

        # 2) audioLevelDeltaDb --------------------------------------------------
        l_entry = energy.get(left_id)
        r_entry = energy.get(right_id)
        l_med = l_entry[1].get("medianDb") if l_entry else None
        r_med = r_entry[1].get("medianDb") if r_entry else None
        if isinstance(l_med, (int, float)) and isinstance(r_med, (int, float)):
            metrics["audioLevelDeltaDb"] = _metric(
                round(float(r_med) - float(l_med), 2),
                status="value",
                evidence_refs=(
                    f"raw/audio-energy.json#shots[{l_entry[0]}]",
                    f"raw/audio-energy.json#shots[{r_entry[0]}]",
                ),
                unit="dB",
            )
        else:
            metrics["audioLevelDeltaDb"] = _metric(None, status="unknown")

        # 3) cameraMotionChange ---------------------------------------------------
        lc = camera.get(left_id)
        rc = camera.get(right_id)
        l_move = lc[1].get("cameraMovement") if lc else None
        r_move = rc[1].get("cameraMovement") if rc else None
        if l_move is not None or r_move is not None:
            metrics["cameraMotionChange"] = _metric(
                {"left": l_move, "right": r_move},
                status="value",
                evidence_refs=(
                    f"raw/camera-motion.json#shots[{lc[0]}]",
                    f"raw/camera-motion.json#shots[{rc[0]}]",
                ),
            )
        else:
            metrics["cameraMotionChange"] = _metric(None, status="unknown")

        # 4) audioCutAligned --------------------------------------------------------
        if audio_boundaries:
            aligned = any(
                abs(b - boundary_ms) <= AUDIO_CUT_ALIGN_TOLERANCE_MS
                for b in audio_boundaries
            )
            metrics["audioCutAligned"] = _metric(
                aligned,
                status="value",
                evidence_refs=("raw/audio-cuts.json#boundaries",),
            )
        else:
            metrics["audioCutAligned"] = _metric(None, status="unknown")

        # 5) speechGapMs / speechSpansBoundary ---------------------------------------
        gap_metric, spans_metric, speech_refs = _speech_metrics(
            segments, boundary_ms=boundary_ms
        )
        metrics["speechGapMs"] = gap_metric
        metrics["speechSpansBoundary"] = spans_metric
        gap_value = gap_metric.get("value")
        if isinstance(gap_value, int) and gap_value >= SPEECH_GAP_REVIEW_MS:
            review_reasons.append(_SPEECH_GAP_REVIEW)

        # 6) musicContinuity ----------------------------------------------------------
        lm = music.get(left_id)
        rm = music.get(right_id)
        l_music = lm[1].get("state") if lm else None
        r_music = rm[1].get("state") if rm else None
        if l_music is not None or r_music is not None:
            metrics["musicContinuity"] = _metric(
                {"left": l_music, "right": r_music},
                status="value",
                evidence_refs=(
                    f"raw/music-flags.json#shots[{lm[0]}]",
                    f"raw/music-flags.json#shots[{rm[0]}]",
                ),
            )
        else:
            metrics["musicContinuity"] = _metric(None, status="unknown")

        # 7. 可选语义层（MiMo 文本模型）：先记 payload，稍后并发执行 --------
        if model_service is None:
            semantic: dict | None = {
                "status": "unknown",
                "reason": "语义模型未配置（textModel 三要素缺失），语义层未运行",
            }
        else:
            frame_notes = {
                "left": (
                    left_frame.get("reason")
                    if isinstance(left_frame.get("reason"), str)
                    else None
                ),
                "right": (
                    right_frame.get("reason")
                    if isinstance(right_frame.get("reason"), str)
                    else None
                ),
            }
            semantic = None  # 占位：并发阶段按序回填
            semantic_payloads.append(
                (
                    index - 1,
                    _model_payload(
                        left, right, boundary_ms, metrics, segments, frame_notes
                    ),
                )
            )

        relations.append(
            {
                "pairID": pair_id,
                "leftShotID": left_id,
                "rightShotID": right_id,
                "boundaryMs": boundary_ms,
                "evidence": {
                    "leftExitFrame": left_frame
                    or {"status": "unavailable", "reason": "未生成"},
                    "rightEntryFrame": right_frame
                    or {"status": "unavailable", "reason": "未生成"},
                },
                "metrics": metrics,
                "semantic": semantic,
                "review": {
                    "needsReview": bool(review_reasons),
                    "reviewReasons": review_reasons,
                },
            }
        )

    # 8. 并发执行语义调用（保序；失败/解析错误只影响对应 pair） -------------
    if model_service is not None and semantic_payloads:
        concurrency = max(
            1,
            int(
                config.get("reviewTimeline", {}).get("semanticConcurrency", 4)
            ),
        )

        def _evaluate(item: tuple[int, dict]) -> tuple[int, dict, list[str]]:
            rel_index, payload = item
            try:
                raw_text = model_service.analyze_pair(payload)
                parsed = parse_pair_semantics(
                    raw_text,
                    ref_base=f"raw/shot-relations.json#relations[{rel_index}]",
                )
                semantic = {
                    "status": "complete",
                    "fields": parsed["fields"],
                    "raw": raw_text,
                }
                if parsed.get("issues"):
                    semantic["issues"] = parsed["issues"]
                # 静帧不足以判断的字段诚实落 unknown 并在检查器展示；
                # 不触发 needsReview（否则 62/62 全量复核失去筛选价值）。
                return rel_index, semantic, []
            except PairSemanticParseError as exc:
                return rel_index, {
                    "status": "failed",
                    "reason": str(exc),
                    "raw": None,
                }, []
            except Exception as exc:  # services 层已二分；此处兜底显式化
                return rel_index, {
                    "status": "failed",
                    "reason": f"语义服务失败：{exc}",
                }, []

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            for rel_index, semantic, extra_reasons in executor.map(
                _evaluate, semantic_payloads
            ):
                relations[rel_index]["semantic"] = semantic
                if extra_reasons:
                    relations[rel_index]["review"]["reviewReasons"].extend(
                        extra_reasons
                    )
                relations[rel_index]["review"]["needsReview"] = bool(
                    relations[rel_index]["review"]["reviewReasons"]
                )
                if semantic.get("status") == "failed":
                    semantic_failed += 1
                    warnings.append(
                        f"{relations[rel_index]['pairID']} 语义失败："
                        f"{semantic.get('reason', '')}"
                    )

    doc: dict = {
        "schemaVersion": 1,
        "status": "partial" if semantic_failed else "complete",
        "sourceRevisionID": source_revision_id,
        "analysis": {
            "method": (
                "adjacent-pair-deterministic-metrics"
                + ("+text-model-semantics" if model_service is not None else "")
            ),
            "algorithmVersion": SHOT_RELATIONS_VERSION,
            "transitionEvidenceVersion": TRANSITION_EVIDENCE_VERSION,
            "reviewTimelineVersion": REVIEW_TIMELINE_VERSION,
            "pairCount": len(relations),
            "modelService": (
                model_service.marker() if model_service is not None else "none"
            ),
        },
        "relations": relations,
    }
    if warnings:
        doc["warnings"] = warnings
    return doc


def build_shot_relations_stub(
    shots: list[dict], config: dict, source_revision_id: str, reason: str
) -> dict:
    """``--skip build_shot_relations`` 降级 stub：pair 集合保留、指标全 unknown。"""
    relations = []
    for index in range(1, len(shots)):
        left = shots[index - 1]
        right = shots[index]
        relations.append(
            {
                "pairID": f"{left['shotID']}--{right['shotID']}",
                "leftShotID": str(left["shotID"]),
                "rightShotID": str(right["shotID"]),
                "boundaryMs": int(left["finalEndMs"]),
                "evidence": {
                    "leftExitFrame": {"status": "unavailable", "reason": reason},
                    "rightEntryFrame": {"status": "unavailable", "reason": reason},
                },
                "metrics": {},
                "semantic": {"status": "unknown", "reason": reason},
                "review": {"needsReview": False, "reviewReasons": []},
            }
        )
    return {
        "schemaVersion": 1,
        "status": "failed",
        "sourceRevisionID": source_revision_id,
        "analysis": {
            "method": "adjacent-pair-deterministic-metrics",
            "algorithmVersion": SHOT_RELATIONS_VERSION,
            "pairCount": len(relations),
            "modelService": "none",
        },
        "relations": relations,
    }
