"""analysis.asr_stage — Phase 1 ASR 阶段（docs/03 §2.7、docs/01 §7.1）。

职责：

- 对 analyzedRange 整片调用一次 ASR 服务（不逐镜头请求），把归一化后的
  ``ASRResult.segments`` 写成 ``asr.json`` 结构（service/status/transcript）。
- 服务未配置（``service=None``）或抛 :class:`CapabilityUnavailableError`
  → M1 stub 语义：``status=skipped`` + note。
- 服务抛其他异常 → ``status=failed`` + 诊断（不向上抛，docs/03 §7 降级矩阵）。
- segments 清洗：按 startMs 升序（乱序记 warning 后排序）；``start < end``
  不成立的丢弃并记 warning；越出 analyzedRange 的 clamp 并记 warning，
  clamp 后为空区间的丢弃。

镜头 speech 派生（:func:`shot_speech`）：

- 与 shot 区间有正交集的 segments 按原文顺序拼接；
- 边界重叠句按交集占该 segment 时长比例 >= 0.5 裁定归属（CALIBRATION）；
- 无交集返回 None（由 Observation 层落 unknown，绝不落 absent）。
"""

from __future__ import annotations

from pathlib import Path

from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
from memoloupe.core.errors import CapabilityUnavailableError
from memoloupe.services.asr import ASRRequest, ASRService

ASR_STAGE_VERSION = "asr.v1"

#: CALIBRATION：边界重叠句归属判定的最小交集比例（交集时长 / segment 时长）。
BOUNDARY_ATTRIBUTION_RATIO = 0.5


def _skipped(note: str) -> dict:
    """asr.json 降级产物：服务未配置/不可用（M1 stub 语义）。"""
    return {
        "service": "asr",
        "status": "skipped",
        "transcript": {"segments": []},
        "note": note,
    }


def _failed(note: str, exc: Exception) -> dict:
    """asr.json 失败产物：保留脱敏诊断，不抛异常中断 Phase 1。"""
    return {
        "service": "asr",
        "status": "failed",
        "transcript": {"segments": []},
        "note": note,
        "error": {"type": type(exc).__name__, "message": str(exc)[:500]},
    }


def _normalize_segments(
    raw_segments: object, start_ms: int, end_ms: int
) -> tuple[list[dict], list[str]]:
    """清洗供应商 segments：排序、合法性过滤、越界 clamp。返回 (segments, warnings)。"""
    warnings: list[str] = []
    if not isinstance(raw_segments, (list, tuple)):
        return [], [f"segments 不是数组：{type(raw_segments).__name__}，已按空处理"]
    cleaned: list[dict] = []
    for index, seg in enumerate(raw_segments):
        if not isinstance(seg, dict):
            warnings.append(f"segments[{index}] 不是对象，已丢弃")
            continue
        seg_start, seg_end, text = seg.get("startMs"), seg.get("endMs"), seg.get("text")
        if not (
            isinstance(seg_start, int)
            and not isinstance(seg_start, bool)
            and isinstance(seg_end, int)
            and not isinstance(seg_end, bool)
            and isinstance(text, str)
        ):
            warnings.append(f"segments[{index}] 缺少合法 startMs/endMs/text，已丢弃")
            continue
        if seg_start >= seg_end:
            warnings.append(
                f"segments[{index}] 区间非法（startMs={seg_start} >= endMs={seg_end}），已丢弃"
            )
            continue
        clamped_start = max(seg_start, start_ms)
        clamped_end = min(seg_end, end_ms)
        if clamped_start >= clamped_end:
            warnings.append(
                f"segments[{index}] 完全在 analyzedRange 外"
                f"（[{seg_start}, {seg_end})），已丢弃"
            )
            continue
        if clamped_start != seg_start or clamped_end != seg_end:
            warnings.append(
                f"segments[{index}] 越界已 clamp：[{seg_start}, {seg_end}) → "
                f"[{clamped_start}, {clamped_end})"
            )
        entry: dict = {
            "startMs": clamped_start,
            "endMs": clamped_end,
            "text": text,
            "speaker": seg.get("speaker"),
            "confidence": seg.get("confidence"),
        }
        cleaned.append(entry)
    ordered = sorted(cleaned, key=lambda s: (s["startMs"], s["endMs"]))
    if [id(a) for a in ordered] != [id(b) for b in cleaned]:
        warnings.append("segments 非 startMs 升序，已按原文排序")
    return ordered, warnings


def run_asr_stage(
    source: Path, media: dict, config: dict, service: ASRService | None = None
) -> dict:
    """执行 ASR 阶段，返回符合 schemas/asr.json 的 dict（由调用方写盘）。

    ``service=None``、无音轨或 :class:`CapabilityUnavailableError` → skipped；
    服务抛其他异常 → failed + 诊断；成功 → complete。
    """
    media_source = media.get("source", {}) if isinstance(media, dict) else {}
    analyzed = media_source.get("analyzedRange") or {}
    start_ms = int(analyzed.get("startMs", 0))
    end_ms = int(analyzed.get("endMs", media_source.get("durationMs", 0)))

    if not media_source.get("audioTracks"):
        return _skipped("无音轨，ASR 不适用")
    if service is None:
        return _skipped("ASR 服务未配置（M1）")

    asr_cfg = config.get("asr", {}) if isinstance(config, dict) else {}
    language = asr_cfg.get("language")
    request = ASRRequest(
        language=str(language) if isinstance(language, str) and language else None,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    try:
        result = service.transcribe(Path(source), request)
    except CapabilityUnavailableError as exc:
        return _skipped(f"ASR 服务不可用：{exc.reason or exc.capability}")
    except Exception as exc:
        return _failed(f"ASR 服务调用失败：{type(exc).__name__}", exc)

    segments, warnings = _normalize_segments(result.segments, start_ms, end_ms)
    text = " ".join(seg["text"].strip() for seg in segments if seg["text"].strip())
    doc: dict = {
        "service": "asr",
        "status": "complete",
        "transcript": {"text": text, "segments": segments},
        "analyzedRange": {"startMs": start_ms, "endMs": end_ms},
        "stageVersion": ASR_STAGE_VERSION,
    }
    if result.raw_extras:
        doc["rawExtras"] = result.raw_extras
    if warnings:
        doc["warnings"] = warnings
    validate_artifact(ArtifactName.ASR, doc)
    return doc


def shot_speech_segments(
    segments: list[dict], shot_start_ms: int, shot_end_ms: int
) -> list[tuple[int, dict]]:
    """选出归属于 [shot_start_ms, shot_end_ms) 的 segments，返回 (原文下标, segment)。

    归属规则（docs/03 §2.7）：正交集即候选；完全包含直接归属；边界重叠句
    要求交集时长 / segment 时长 >= BOUNDARY_ATTRIBUTION_RATIO（跨镜头句可被
    多个镜头引用）。保持原文顺序，不改写文本。
    """
    hits: list[tuple[int, dict]] = []
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            continue
        seg_start, seg_end = segment.get("startMs"), segment.get("endMs")
        text = segment.get("text")
        if not (
            isinstance(seg_start, int)
            and isinstance(seg_end, int)
            and isinstance(text, str)
            and text.strip()
        ):
            continue
        intersection = min(seg_end, shot_end_ms) - max(seg_start, shot_start_ms)
        if intersection <= 0:
            continue
        contained = seg_start >= shot_start_ms and seg_end <= shot_end_ms
        seg_duration = seg_end - seg_start
        if (
            not contained
            and seg_duration > 0
            and intersection / seg_duration < BOUNDARY_ATTRIBUTION_RATIO
        ):
            continue
        hits.append((index, segment))
    return hits


def shot_speech(
    segments: list[dict], shot_start_ms: int, shot_end_ms: int
) -> str | None:
    """镜头 speech 文本：归属 segments 按原文顺序空格拼接；无交集返回 None。"""
    hits = shot_speech_segments(segments, shot_start_ms, shot_end_ms)
    if not hits:
        return None
    return " ".join(str(seg["text"]).strip() for _, seg in hits)
