"""render.shot_html — shot-analysis.html 渲染器（docs/04 §2/§3/§5/§6）。

流程固定为：读 output-dir 的 raw/*.json（缺失容忍为 None）→ 经
:func:`memoloupe.analysis.resolvers.build_observations_with_review` 生成
Observation 并收集 review 理由 → 应用 corrections overlay
（:mod:`memoloupe.render.corrections`，docs/02 §6 渲染顺序）→ 映射到
``templates/shot-analysis.html`` 骨架（占位符字符串替换）→ 原子写入。

- 所有模型/检测原文经 ``html.escape`` 后才进入 HTML；
- CSS/JS 由模板固定内联，绝不动态拼接用户内容（CSP 例外因此安全）；
- 注入 JS 的 JSON 数据经 ``json.dumps`` 并转义尖括号/``&``，防止形成脚本标签；
- 媒体一律相对路径；缺 clips/SHxxxx.mp4 时播放按钮禁用；
- 受控词表字段渲染 ``<select>`` 内联编辑控件，自由文本字段渲染 ``<input>``；
- 命中人工修正的单元格带 ``data-source="human"`` 与 ``data-original-value``；
- 模板替换完成后若仍有占位符残留，抛 :class:`ArtifactError`。
"""

from __future__ import annotations

from datetime import datetime, timezone
import html
import importlib
import json
import os
import shutil
from pathlib import Path
from urllib.parse import quote

from memoloupe.analysis.observations import Observation, Source, ValueState
from memoloupe.analysis.resolvers import DEFAULT_RESOLVERS, build_observations_with_review
from memoloupe.analysis.vocabulary import FieldRule, Vocabulary, load_vocabulary
from memoloupe.core.atomic_io import read_json, write_text_atomic
from memoloupe.core.errors import ArtifactError, ContractError
from memoloupe.validate.html_contract import DOCUMENT_STATUSES

SHOT_RENDER_VERSION = "render.v3"
CONTRACT_VERSION = "1.0"
DOCUMENT_TYPE = "shotAnalysis"

_TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "templates" / "shot-analysis.html"
_LOGO_SOURCE = Path(__file__).resolve().parents[3] / "assets" / "brand" / "memoloupe-logo.png"

#: 渲染器读取的 raw 逻辑名（缺失时容忍为 None）。
RAW_FILES: tuple[str, ...] = (
    "media",
    "shots",
    "frame-evidence",
    "audio-energy",
    "quality-flags",
    "music-flags",
    "asr",
    "unified-media",
    "camera-motion",
    "story-blocks",
)

#: 非 value 状态的固定可见文案（docs/04 §3.3：absent 与 absent-claimed 必须不同）。
_STATE_TEXT: dict[ValueState, str] = {
    ValueState.ABSENT: "无（确定性检测）",
    ValueState.ABSENT_CLAIMED: "模型未发现",
    ValueState.UNKNOWN: "未知",
}

_FIELD_LABELS: dict[str, str] = {
    "audio.speech": "对白/旁白",
    "audio.bgmPresence": "BGM 是否存在",
    "audio.energy": "音量能量",
    "quality.flags": "质量风险",
    "visual.cameraMovement": "运镜",
    "visual.movementIntensity": "运动强度",
    "visual.contentSummary": "镜头内容摘要",
    "editing.transition": "剪辑转场",
    "visual.subjects": "画面主体",
    "visual.actions": "主体动作",
    "visual.setting": "场景环境",
    "visual.props": "道具",
    "visual.framing": "景别",
    "visual.cameraAngle": "机位角度",
    "visual.composition": "构图",
    "visual.viewpoint": "观看关系",
    "visual.brightness": "亮度",
    "visual.contrast": "对比度",
    "visual.lightingSource": "光源",
    "visual.perceivedColorTemperature": "色温",
    "visual.saturation": "饱和度",
    "visual.depthOfField": "景深",
    "visual.imageTexture": "成像质感",
    "visual.dominantColor": "主色",
    "visual.perceivedLensFeel": "镜头透视感",
    "function.sourceMedium": "素材形态",
    "function.subjectEmotion": "人物情绪",
    "function.shotTone": "镜头语气",
    "audio.bgmStyle": "BGM 风格",
    "audio.soundEvents": "声音事件",
    "components.nonTextOverlayEvents": "非文字图层/合成",
}

_FIELD_GROUPS: tuple[dict[str, object], ...] = (
    {
        "id": "core",
        "title": "核心审片",
        "description": "先判断镜头讲了什么、是否有声音、怎么接、有没有明显风险。",
        "default_open": True,
        "fields": (
            "visual.contentSummary",
            "audio.speech",
            "audio.bgmPresence",
            "editing.transition",
            "visual.cameraMovement",
            "visual.movementIntensity",
            "quality.flags",
        ),
    },
    {
        "id": "visual-action",
        "title": "画面内容与调度",
        "description": "主体、动作、场景、道具和基础摄影调度。",
        "default_open": False,
        "fields": (
            "visual.subjects",
            "visual.actions",
            "visual.setting",
            "visual.props",
            "visual.framing",
            "visual.cameraAngle",
            "visual.composition",
            "visual.viewpoint",
        ),
    },
    {
        "id": "visual-style",
        "title": "视觉风格",
        "description": "光线、色彩、景深、质感和镜头透视感。",
        "default_open": False,
        "fields": (
            "visual.brightness",
            "visual.contrast",
            "visual.lightingSource",
            "visual.perceivedColorTemperature",
            "visual.saturation",
            "visual.depthOfField",
            "visual.imageTexture",
            "visual.dominantColor",
            "visual.perceivedLensFeel",
        ),
    },
    {
        "id": "audio-detail",
        "title": "声音层",
        "description": "能量、BGM 风格和可听声音事件；BGM 有无仍以确定性检测为准。",
        "default_open": False,
        "fields": (
            "audio.energy",
            "audio.bgmStyle",
            "audio.soundEvents",
        ),
    },
    {
        "id": "function",
        "title": "功能与情绪",
        "description": "镜头在短片中的功能、人物情绪和整体语气。",
        "default_open": False,
        "fields": (
            "function.sourceMedium",
            "function.subjectEmotion",
            "function.shotTone",
        ),
    },
    {
        "id": "post",
        "title": "文字与后期图层",
        "description": "非文字后期图层、合成事件；文字字段会在后续完整组件呈现中继续展开。",
        "default_open": False,
        "fields": (
            "components.nonTextOverlayEvents",
        ),
    },
)

_FIELD_GROUP_BY_FIELD: dict[str, dict[str, object]] = {
    field: group
    for group in _FIELD_GROUPS
    for field in group["fields"]  # type: ignore[index]
}


def _load_raws(out_dir: Path) -> dict[str, dict | None]:
    raws: dict[str, dict | None] = {}
    for name in RAW_FILES:
        try:
            raws[name] = read_json(out_dir / "raw" / f"{name}.json")
        except ContractError:
            raws[name] = None
    return raws


def _load_corrections(out_dir: Path):
    """加载 corrections overlay，返回 ``(module, Corrections)`` 或 None。

    corrections 文件不存在时返回 None（无 overlay，文档状态回落到 ``status``
    参数）；``render.corrections`` 模块尚不可用时同样返回 None——显式降级为
    无 overlay 渲染，而不是抛错中断渲染阶段。
    """
    corr_path = out_dir / "corrections" / f"{DOCUMENT_TYPE}.json"
    if not corr_path.is_file():
        return None
    try:
        # import_module 只查 sys.modules/文件，避免 `from pkg import sub` 缓存
        # 包属性导致测试替身泄漏到其他用例。
        corrections_mod = importlib.import_module("memoloupe.render.corrections")
    except ImportError:
        return None
    return corrections_mod, corrections_mod.load_corrections(out_dir, DOCUMENT_TYPE)


def _timecode(ms: int) -> str:
    total_s, rem = divmod(int(ms), 1000)
    hours, rem_s = divmod(total_s, 3600)
    minutes, seconds = divmod(rem_s, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{rem:03d}"
    return f"{minutes:02d}:{seconds:02d}.{rem:03d}"


def _frame_refs(raws: dict[str, dict | None], out_dir: Path) -> dict[str, str]:
    """shotID -> 代表帧相对路径；文件不存在时不引用。"""
    doc = raws.get("frame-evidence")
    result: dict[str, str] = {}
    if not doc:
        return result
    frames = doc.get("frames")
    if not isinstance(frames, list):
        return result
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        shot_id, file_ref = frame.get("shotID"), frame.get("fileRef")
        if (
            isinstance(shot_id, str)
            and isinstance(file_ref, str)
            and shot_id not in result
            and (out_dir / file_ref).is_file()
        ):
            result[shot_id] = file_ref
    return result


def _clip_src(out_dir: Path, shot_id: str) -> str | None:
    clip_path = out_dir / "clips" / f"{shot_id}.mp4"
    return f"clips/{shot_id}.mp4" if clip_path.is_file() else None


def _full_video_src(raws: dict[str, dict | None], out_dir: Path) -> str | None:
    media = raws.get("media")
    if not media:
        return None
    source_path = media.get("source", {}).get("sourcePath")
    if not isinstance(source_path, str) or not source_path:
        return None
    source = Path(source_path)
    if not source.is_file():
        return None
    rel = os.path.relpath(source, out_dir)
    return quote(Path(rel).as_posix())


def _copy_logo_asset(out_dir: Path) -> None:
    """把品牌 logo 复制到 out_dir/assets/，供 HTML 以相对路径引用（幂等）。"""
    target = out_dir / "assets" / "memoloupe-logo.png"
    if target.is_file() and target.read_bytes() == _LOGO_SOURCE.read_bytes():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_LOGO_SOURCE, target)


def _merged_review_reasons(shot: dict, review_reasons: list[str]) -> list[str]:
    merged = list(review_reasons)
    if shot.get("needsReview"):
        merged.append("shots.json 标记 needsReview")
    return merged


def _shot_duration_ms(shot: dict) -> int:
    duration = shot.get("durationMs")
    if isinstance(duration, int) and duration > 0:
        return duration
    start_ms = shot.get("finalStartMs")
    end_ms = shot.get("finalEndMs")
    if isinstance(start_ms, int) and isinstance(end_ms, int) and end_ms > start_ms:
        return end_ms - start_ms
    return 0


def _music_by_shot(raws: dict[str, dict | None]) -> dict[str, str]:
    doc = raws.get("music-flags")
    result: dict[str, str] = {}
    if not doc:
        return result
    shots = doc.get("shots")
    if not isinstance(shots, list):
        return result
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        shot_id = shot.get("shotID")
        state = shot.get("state")
        if isinstance(shot_id, str) and isinstance(state, str):
            result[shot_id] = state
    return result


def _music_tally(raws: dict[str, dict | None]) -> dict[str, int]:
    doc = raws.get("music-flags")
    if not doc:
        return {}
    tally = doc.get("stateTally")
    if not isinstance(tally, dict):
        return {}
    return {str(k): int(v) for k, v in tally.items() if isinstance(v, int)}


def _unified_batch_status(raws: dict[str, dict | None]) -> tuple[str, int]:
    doc = raws.get("unified-media")
    if not doc:
        return "missing", 0
    batches = doc.get("batches")
    if not isinstance(batches, list):
        return "unknown", 0
    statuses = [b.get("status") for b in batches if isinstance(b, dict)]
    if statuses and all(s == "complete" for s in statuses):
        return "complete", len(statuses)
    if statuses and any(s == "failed" for s in statuses):
        return "partial", len(statuses)
    return "unknown", len(statuses)


def _document_status_label(status: str) -> str:
    return {
        "draft": "未校对",
        "underReview": "校对中",
        "confirmed": "已确认",
        "outdated": "需更新",
    }.get(status, status)


def _story_status_label(status: str) -> str:
    return {
        "complete": "已完成",
        "scaffold": "已生成初稿",
        "partial": "部分完成",
        "failed": "生成失败",
    }.get(status, status or "待生成")


def _story_role_label(role: object) -> str:
    return {
        "hook": "开场吸引",
        "context": "背景铺垫",
        "promise": "建立期待",
        "problem": "提出问题",
        "development": "过程展开",
        "proof": "补充证明",
        "turn": "情绪/信息转折",
        "payoff": "高潮兑现",
        "resolution": "收束总结",
        "custom": "自定义作用",
        "unknown": "作用待确认",
    }.get(str(role or "unknown"), str(role or "作用待确认"))


def _summary_html(
    document_status: str,
    shots: list[dict],
    review_reasons_by_shot: dict[str, list[str]],
    raws: dict[str, dict | None],
) -> str:
    review_count = sum(
        1
        for shot in shots
        if _merged_review_reasons(shot, review_reasons_by_shot.get(str(shot.get("shotID", "")), []))
    )
    total_duration = sum(_shot_duration_ms(shot) for shot in shots)
    music_tally = _music_tally(raws)
    music_value = f"{music_tally.get('music', 0)} 已识别 · {music_tally.get('unknown', 0)} 待确认"
    quality_doc = raws.get("quality-flags") or {}
    flagged_quality = quality_doc.get("flaggedShotCount")
    quality_value = str(flagged_quality) if isinstance(flagged_quality, int) else "待确认"
    unified_status, batch_count = _unified_batch_status(raws)
    unified_label = {
        "complete": "已完成",
        "partial": "部分完成",
        "missing": "未运行",
        "unknown": "待确认",
    }.get(unified_status, "待确认")
    cards = [
        (
            "镜头总数",
            str(len(shots)),
            "raw/shots.json#shots",
            "badge-outline",
            "已切分",
        ),
        (
            "需复核镜头",
            str(review_count),
            "resolver review_reasons + raw/shots.json#shots[*].needsReview",
            "badge-warning" if review_count else "badge-success",
            "优先检查",
        ),
        (
            "全片时长",
            _timecode(total_duration),
            "raw/shots.json#shots[*].finalStartMs/finalEndMs",
            "badge-outline",
            "校对范围",
        ),
        (
            "背景音乐",
            music_value,
            "raw/music-flags.json#stateTally",
            "badge-outline",
            "检测结果",
        ),
        (
            "画质问题",
            quality_value,
            "raw/quality-flags.json#flaggedShotCount",
            "badge-warning" if quality_value not in {"0", "待确认"} else "badge-outline",
            "需要留意",
        ),
        (
            "视频理解",
            f"{unified_label} · {batch_count} 组",
            "raw/unified-media.json#batches",
            "badge-success" if unified_status == "complete" else "badge-warning",
            "语义分析",
        ),
    ]
    parts = [
        '<section id="shot-summary" class="card" aria-label="镜头分析总览">',
        '<div class="card-header"><div>',
        '<h2 class="card-title">审片总览</h2>',
        f'<p class="card-description">当前状态：{html.escape(_document_status_label(document_status))}。先看需复核镜头，再进入时间线逐镜检查。</p>',
        '</div><span class="badge badge-outline">总览</span></div>',
        '<div class="summary-grid">',
    ]
    for label, value, source, badge_class, note in cards:
        parts.append(
            '<article class="metric-card" '
            f'data-evidence-refs="{html.escape(source)}">'
            f'<p class="metric-label">{html.escape(label)}</p>'
            f'<p class="metric-value">{html.escape(value)}</p>'
            f'<span class="badge {badge_class}">{html.escape(note)}</span>'
            '</article>'
        )
    parts.append("</div></section>")
    return "".join(parts)


def _story_blocks(raws: dict[str, dict | None]) -> list[dict]:
    story = raws.get("story-blocks")
    if not story:
        return []
    blocks = story.get("blocks")
    if not isinstance(blocks, list):
        return []
    return [block for block in blocks if isinstance(block, dict)]


def _story_slots(raws: dict[str, dict | None]) -> list[dict]:
    story = raws.get("story-blocks")
    if not story:
        return []
    slots = story.get("slots")
    if not isinstance(slots, list):
        return []
    return [slot for slot in slots if isinstance(slot, dict)]


def _story_slot_map(slots: list[dict]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for slot in slots:
        block_ids = slot.get("blockIDs")
        if not isinstance(block_ids, list):
            continue
        for block_id in block_ids:
            if isinstance(block_id, str):
                result.setdefault(block_id, []).append(slot)
    return result


def _story_context_by_shot(raws: dict[str, dict | None]) -> dict[str, dict]:
    """shotID -> story 摘要；用于右侧 Sidebar 的故事归属卡片。"""
    blocks = _story_blocks(raws)
    slots_by_block = _story_slot_map(_story_slots(raws))
    result: dict[str, dict] = {}
    for block in blocks:
        block_id = str(block.get("storyBlockID") or "")
        if not block_id:
            continue
        shot_ids = block.get("shotIDs")
        if not isinstance(shot_ids, list):
            continue
        start_ms, end_ms = block.get("startMs"), block.get("endMs")
        slots = slots_by_block.get(block_id, [])
        context = {
            "blockID": block_id,
            "blockTitle": str(block.get("blockTitle") or block_id),
            "coreContent": str(block.get("coreContent") or "待确认"),
            "primaryRole": _story_role_label(block.get("primaryRole")),
            "narrativeDensity": str(block.get("narrativeDensity") or "密度待确认"),
            "visualIndependence": str(block.get("visualIndependence") or "静音可读性待确认"),
            "timecode": (
                f"{_timecode(start_ms)} – {_timecode(end_ms)}"
                if isinstance(start_ms, int) and isinstance(end_ms, int)
                else ""
            ),
            "slots": [
                {
                    "slotID": str(slot.get("slotID") or ""),
                    "slotTitle": str(slot.get("slotTitle") or "结构段"),
                    "slotType": str(slot.get("slotType") or "待确认"),
                }
                for slot in slots
            ],
        }
        for shot_id in shot_ids:
            if isinstance(shot_id, str):
                result[shot_id] = context
    return result


def _story_timeline_band_html(
    raws: dict[str, dict | None],
    shots: list[dict],
    out_dir: Path,
) -> str:
    story = raws.get("story-blocks")
    blocks = _story_blocks(raws)
    if not story or not blocks:
        return ""
    slots = _story_slots(raws)
    slots_by_block = _story_slot_map(slots)
    shots_by_id = {str(shot.get("shotID", "")): shot for shot in shots}
    timeline_ms = sum(_shot_duration_ms(shot) for shot in shots) or 1
    parts = [
        '<div id="story-timeline-band" class="story-timeline-band" '
        'aria-label="故事轨道">',
        '<div class="timeline-lane-label">',
        '<span>故事</span>',
        f'<small>{len(blocks)} 段 · {_story_status_label(str(story.get("status") or ""))}</small>',
        "</div>",
        '<div class="story-segment-track" role="list">',
    ]
    for index, block in enumerate(blocks, start=1):
        block_id = str(block.get("storyBlockID") or f"B{index:04d}")
        shot_ids = [sid for sid in block.get("shotIDs", []) if isinstance(sid, str)]
        first_shot_id = shot_ids[0] if shot_ids else ""
        first_shot = shots_by_id.get(first_shot_id, {})
        clip_src = _clip_src(out_dir, first_shot_id) if first_shot_id else None
        start_ms = block.get("startMs")
        end_ms = block.get("endMs")
        duration = (
            max(0, end_ms - start_ms)
            if isinstance(start_ms, int) and isinstance(end_ms, int)
            else 0
        )
        width = max((duration / timeline_ms) * 100, 4.0)
        range_text = (
            f"{_timecode(start_ms)} – {_timecode(end_ms)}"
            if isinstance(start_ms, int) and isinstance(end_ms, int)
            else "时间待确认"
        )
        block_slots = slots_by_block.get(block_id, [])
        slot_text = " / ".join(
            str(slot.get("slotTitle") or slot.get("slotID") or "结构段")
            for slot in block_slots
        ) or "未归入结构段"
        src_attr = f' data-clip-src="{html.escape(clip_src)}"' if clip_src else ""
        shot_attr = f' data-shot-id="{html.escape(first_shot_id)}"' if first_shot_id else ""
        start_attr = (
            f' data-start-ms="{first_shot.get("finalStartMs")}"'
            if isinstance(first_shot.get("finalStartMs"), int)
            else ""
        )
        end_attr = (
            f' data-end-ms="{first_shot.get("finalEndMs")}"'
            if isinstance(first_shot.get("finalEndMs"), int)
            else ""
        )
        disabled = "" if clip_src else " disabled"
        parts.append(
            '<button type="button" class="story-segment shot-jump" role="listitem" '
            f'style="--story-width: {width:.2f}%" data-layer-id="{html.escape(block_id)}"'
            f' data-layer-shot-ids="{html.escape(" ".join(shot_ids))}"'
            f'{shot_attr}{src_attr}{start_attr}{end_attr}{disabled} '
            f'title="{html.escape(str(block.get("coreContent") or ""))}" '
            f'aria-label="查看故事段 {html.escape(str(block.get("blockTitle") or block_id))}">'
            '<span class="story-segment-topline">'
            f'<span class="badge badge-outline">{html.escape(block_id)}</span>'
            f'<span class="story-time">{html.escape(range_text)}</span>'
            "</span>"
            f'<strong>{html.escape(str(block.get("blockTitle") or block_id))}</strong>'
            '<span class="story-segment-meta">'
            f'{html.escape(slot_text)} · {html.escape(_story_role_label(block.get("primaryRole")))} · {len(shot_ids)} 镜头'
            "</span>"
            "</button>"
        )
    parts.append("</div></div>")
    return "".join(parts)


def _timeline_html(
    shots: list[dict],
    review_reasons_by_shot: dict[str, list[str]],
    frame_refs: dict[str, str],
    out_dir: Path,
    raws: dict[str, dict | None],
) -> str:
    total_duration = sum(_shot_duration_ms(shot) for shot in shots) or 1
    music_states = _music_by_shot(raws)
    parts = [
        '<section id="shot-timeline" class="timeline-card card" aria-label="镜头时间线">',
        '<div class="card-header"><div>',
        '<h2 class="card-title">镜头与故事时间线</h2>',
        '<p class="card-description">上方是故事段，下方是镜头切分；金色表示需复核，绿色表示有背景音乐，虚线表示声音仍待确认。</p>',
        '</div><span class="badge badge-outline">按时长</span></div>',
        _story_timeline_band_html(raws, shots, out_dir),
        '<div class="shot-timeline-band" aria-label="镜头轨道">',
        '<div class="timeline-lane-label"><span>镜头</span><small>'
        f"{len(shots)} 个切分"
        "</small></div>",
        '<div class="timeline-track" role="list">',
    ]
    filmstrip: list[str] = ['<div class="filmstrip" aria-label="代表帧胶片条">']
    for shot in shots:
        shot_id = str(shot.get("shotID", ""))
        esc_id = html.escape(shot_id)
        duration = _shot_duration_ms(shot)
        width = max((duration / total_duration) * 100, 1.0)
        start_ms = shot.get("finalStartMs")
        end_ms = shot.get("finalEndMs")
        clip_src = _clip_src(out_dir, shot_id)
        reasons = _merged_review_reasons(shot, review_reasons_by_shot.get(shot_id, []))
        music_state = music_states.get(shot_id, "unknown")
        class_names = ["timeline-shot", "shot-jump"]
        if reasons:
            class_names.append("is-review")
        if music_state == "music":
            class_names.append("is-music")
        elif music_state == "unknown":
            class_names.append("is-unknown")
        disabled = "" if clip_src else " disabled"
        src_attr = f' data-clip-src="{html.escape(clip_src)}"' if clip_src else ""
        start_attr = f' data-start-ms="{start_ms}"' if isinstance(start_ms, int) else ""
        end_attr = f' data-end-ms="{end_ms}"' if isinstance(end_ms, int) else ""
        title_bits = [shot_id]
        if isinstance(start_ms, int) and isinstance(end_ms, int):
            title_bits.append(f"{_timecode(start_ms)}–{_timecode(end_ms)}")
        title_bits.append(f"BGM={music_state}")
        if reasons:
            title_bits.append("复核：" + "；".join(reasons))
        parts.append(
            f'<button type="button" class="{" ".join(class_names)}" role="listitem" '
            f'style="--shot-width: {width:.2f}%" data-shot-id="{esc_id}"'
            f'{src_attr}{start_attr}{end_attr}{disabled} '
            f'title="{html.escape(" · ".join(title_bits))}" '
            f'aria-label="跳转到镜头 {esc_id}">{esc_id}</button>'
        )
        frame_ref = frame_refs.get(shot_id)
        if frame_ref is not None:
            filmstrip.append(
                f'<button type="button" class="filmstrip-shot shot-jump" '
                f'data-shot-id="{esc_id}"{src_attr}{start_attr}{end_attr}{disabled} '
                f'aria-label="查看镜头 {esc_id}">'
                f'<img src="{html.escape(frame_ref)}" alt="镜头 {esc_id} 代表帧" loading="lazy">'
                f'<span>{esc_id} · {_timecode(duration)}</span>'
                '</button>'
            )
    parts.append("</div></div>")
    filmstrip.append("</div>")
    parts.extend(filmstrip)
    parts.append("</section>")
    return "".join(parts)


def _shot_inspector_json(
    shots: list[dict],
    observations_by_shot: dict[str, list[Observation]],
    review_reasons_by_shot: dict[str, list[str]],
    frame_refs: dict[str, str],
    out_dir: Path,
    raws: dict[str, dict | None],
) -> str:
    groups = [
        {
            "id": str(group["id"]),
            "title": str(group["title"]),
            "description": str(group["description"]),
            "fields": list(group["fields"]),  # type: ignore[arg-type]
        }
        for group in _FIELD_GROUPS
    ]
    shot_items = []
    story_context = _story_context_by_shot(raws)
    for shot in shots:
        shot_id = str(shot.get("shotID", ""))
        start_ms = shot.get("finalStartMs")
        end_ms = shot.get("finalEndMs")
        duration = _shot_duration_ms(shot)
        grouped_fields: dict[str, list[dict[str, object]]] = {
            str(group["id"]): [] for group in _FIELD_GROUPS
        }
        grouped_fields.setdefault("other", [])
        for obs in observations_by_shot.get(shot_id, []):
            group = _group_for_field(obs.field)
            group_id = str(group["id"])
            grouped_fields.setdefault(group_id, []).append(
                {
                    "field": obs.field,
                    "label": _FIELD_LABELS.get(obs.field, obs.field),
                    "value": _state_plain_text(obs),
                    "state": obs.state.value,
                    "confidence": obs.confidence.value,
                    "source": obs.source.value,
                    "verified": obs.verified,
                    "evidenceRefs": list(obs.evidence_refs),
                }
            )
        shot_items.append(
            {
                "shotID": shot_id,
                "startMs": start_ms if isinstance(start_ms, int) else None,
                "endMs": end_ms if isinstance(end_ms, int) else None,
                "durationMs": duration,
                "timecode": (
                    f"{_timecode(start_ms)} – {_timecode(end_ms)}"
                    if isinstance(start_ms, int) and isinstance(end_ms, int)
                    else ""
                ),
                "durationText": _timecode(duration),
                "frameRef": frame_refs.get(shot_id),
                "clipSrc": _clip_src(out_dir, shot_id),
                "needsReview": bool(
                    _merged_review_reasons(
                        shot,
                        review_reasons_by_shot.get(shot_id, []),
                    )
                ),
                "reviewReasons": _merged_review_reasons(
                    shot,
                    review_reasons_by_shot.get(shot_id, []),
                ),
                "story": story_context.get(shot_id),
                "groups": grouped_fields,
            }
        )
    payload = {"groups": groups, "shots": shot_items}
    return _json_for_script(payload)


def _state_visible_text(obs: Observation) -> str:
    if obs.state == ValueState.VALUE:
        value = obs.value
        if isinstance(value, list):
            return html.escape("、".join(str(item) for item in value) if value else "无标记")
        return html.escape(str(value))
    if obs.state == ValueState.UNMAPPED:
        return "待归类：" + html.escape(str(obs.original_value))
    return _STATE_TEXT[obs.state]


def _state_plain_text(obs: Observation) -> str:
    if obs.state == ValueState.VALUE:
        value = obs.value
        if isinstance(value, list):
            return "、".join(str(item) for item in value) if value else "无标记"
        return str(value)
    if obs.state == ValueState.UNMAPPED:
        return f"待归类：{obs.original_value}"
    return _STATE_TEXT[obs.state]


def _confidence_visible_text(value: str) -> str:
    return {
        "high": "可信度高",
        "medium": "可信度中",
        "low": "可信度低",
        "unknown": "可信度待确认",
    }.get(value, "可信度待确认")


def _initial_edit_value(obs: Observation) -> str:
    """编辑控件的初始值（JS 作为 pendingChanges 的 oldValue）。"""
    if obs.state == ValueState.VALUE and obs.value is not None:
        if isinstance(obs.value, list):
            return "、".join(str(item) for item in obs.value)
        return str(obs.value)
    if obs.state == ValueState.UNMAPPED:
        return str(obs.original_value)
    return ""


def _edit_control_html(obs: Observation, rule: FieldRule | None) -> str:
    """单元格内联编辑控件：受控词表字段 <select>，自由文本字段 <input>。

    控件原生可键盘操作；aria-label 携带 shotID+field。所有值 html.escape。
    """
    esc_field = html.escape(obs.field)
    esc_shot = html.escape(obs.shot_id)
    aria = html.escape(f"编辑 {obs.shot_id} {obs.field}")
    initial = html.escape(_initial_edit_value(obs))
    if rule is None:
        return (
            f'<input type="text" class="cell-edit" data-field="{esc_field}" '
            f'data-shot-id="{esc_shot}" data-initial-value="{initial}" '
            f'value="{initial}" aria-label="{aria}">'
        )
    options = list(rule.values)
    if "unknown" not in options:
        options.append("unknown")
    current: str | None = None
    extra: tuple[str, str] | None = None
    if obs.state == ValueState.VALUE and isinstance(obs.value, str):
        if obs.value in options:
            current = obs.value
        else:
            # 词表外当前值（如 Apple Vision 原始标签）：保留可见且选中。
            extra = (obs.value, f"{obs.value}（词表外当前值）")
    elif obs.state == ValueState.UNKNOWN:
        current = "unknown"
    elif obs.state == ValueState.UNMAPPED:
        # 保留原值并提示映射（docs/04 §8.2：unmapped 应保留可见原始值或修正入口）。
        extra = ("", f"{obs.original_value}（待归类）")
    else:
        extra = ("", _STATE_TEXT[obs.state])
    parts: list[str] = []
    for opt in options:
        selected = " selected" if current == opt else ""
        visible = "待确认" if opt == "unknown" else opt
        parts.append(
            f'<option value="{html.escape(opt)}"{selected}>{html.escape(visible)}</option>'
        )
    if extra is not None:
        parts.append(
            f'<option value="{html.escape(extra[0])}" selected>{html.escape(extra[1])}</option>'
        )
    return (
        f'<select class="cell-edit" data-field="{esc_field}" '
        f'data-shot-id="{esc_shot}" data-initial-value="{initial}" '
        f'aria-label="{aria}">{"".join(parts)}</select>'
    )


def _cell_html(obs: Observation, rule: FieldRule | None) -> str:
    """字段单元格：属性顺序固定（快照稳定），全部值 html.escape。"""
    attrs = (
        f'data-field="{html.escape(obs.field)}" '
        f'data-shot-id="{html.escape(obs.shot_id)}" '
        f'data-value-state="{obs.state.value}" '
        f'data-confidence="{obs.confidence.value}" '
        f'data-evidence-refs="{html.escape(" ".join(obs.evidence_refs))}" '
        f'data-source="{html.escape(obs.source.value)}" '
        f'data-verified="{"true" if obs.verified else "false"}"'
    )
    if obs.source == Source.HUMAN:
        # 人工修正单元格：保留修正前的旧值（docs/04 §5、docs/02 §3.2）。
        original = "" if obs.original_value is None else str(obs.original_value)
        attrs += f' data-original-value="{html.escape(original)}"'
    state_class = f"state-{obs.state.value}"
    esc_field = html.escape(obs.field)
    esc_shot = html.escape(obs.shot_id)
    parts = [f'<span class="cell-text {state_class}">{_state_visible_text(obs)}</span>']
    # confidence=unknown 也必须可见（docs/04 §3.3），但用户界面使用中文状态。
    parts.append(
        f'<span class="cell-confidence">{html.escape(_confidence_visible_text(obs.confidence.value))}</span>'
    )
    if obs.verified:
        parts.append('<span class="cell-verified">已核实</span>')
    parts.append(_edit_control_html(obs, rule))
    checked = " checked" if obs.verified else ""
    parts.append(
        f'<label class="cell-verify"><input type="checkbox" class="verify-toggle" '
        f'data-field="{esc_field}" data-shot-id="{esc_shot}" '
        f'aria-label="核实 {esc_shot} {esc_field}"{checked}> 已核实</label>'
    )
    return f"<td {attrs}>{''.join(parts)}</td>"


def _column_header_html(
    shot: dict,
    review_reasons: list[str],
) -> str:
    shot_id = str(shot.get("shotID", ""))
    start_ms = shot.get("finalStartMs")
    end_ms = shot.get("finalEndMs")
    duration_ms = shot.get("durationMs")
    # needsReview：合并 shots.json 标记与 resolver 报告的 review 理由（D-005）。
    # data-review-reasons 稳定机器语义：JSON 字符串数组，resolver 理由在前，
    # shots.json needsReview=true 时追加标记理由；data-needs-review="true"
    # 当且仅当该数组非空（html_contract 校验此不变量）。
    merged_reasons = _merged_review_reasons(shot, review_reasons)
    needs_review = bool(merged_reasons)
    reasons_attr = html.escape(json.dumps(merged_reasons, ensure_ascii=False))
    esc_id = html.escape(shot_id)

    title_attr = ""
    if review_reasons:
        title_attr = f' title="{html.escape("；".join(review_reasons))}"'
    elif needs_review:
        title_attr = ' title="shots.json 标记 needsReview"'

    lines = [
        f'<th scope="col" data-shot-id="{esc_id}" data-start-ms="{start_ms}" '
        f'data-end-ms="{end_ms}" data-needs-review="{"true" if needs_review else "false"}"'
        f' data-review-reasons="{reasons_attr}"'
        f"{title_attr}>",
        '<div class="shot-head">',
        f'<span class="shot-id">{esc_id}</span>',
    ]
    if isinstance(start_ms, int) and isinstance(end_ms, int):
        lines.append(
            f'<span class="timecode"><code>{_timecode(start_ms)} – {_timecode(end_ms)}</code></span>'
        )
    if isinstance(duration_ms, int):
        lines.append(f'<span class="duration">时长 {duration_ms / 1000:.3f}s</span>')
    if needs_review:
        lines.append('<span class="needs-review-badge">需人工复核</span>')
    lines.append("</div></th>")
    return "".join(lines)


def _field_label_html(field: str) -> str:
    label = _FIELD_LABELS.get(field, field)
    return f'<span class="field-label">{html.escape(label)}</span>'


def _group_for_field(field: str) -> dict[str, object]:
    return _FIELD_GROUP_BY_FIELD.get(
        field,
        {
            "id": "other",
            "title": "其他字段",
            "description": "尚未归入固定业务分类的字段。",
            "default_open": False,
            "fields": (),
        },
    )


def _field_category_nav(groups: list[dict[str, object]]) -> str:
    buttons = [
        '<button type="button" class="category-tab is-active" '
        'data-field-filter="all" aria-selected="true">全部字段</button>'
    ]
    for group in groups:
        group_id = html.escape(str(group["id"]))
        buttons.append(
            '<button type="button" class="category-tab" '
            f'data-field-filter="{group_id}" aria-selected="false">'
            f'{html.escape(str(group["title"]))}</button>'
        )
    return (
        '<div class="field-category-nav" role="tablist" aria-label="字段分类筛选">'
        + "".join(buttons)
        + "</div>"
    )


def _table_html(
    shots: list[dict],
    observations_by_shot: dict[str, list[Observation]],
    review_reasons_by_shot: dict[str, list[str]],
    vocabulary: Vocabulary,
) -> str:
    head_cells = ['<th scope="col">字段 \\ 镜头</th>']
    for shot in shots:
        shot_id = str(shot.get("shotID", ""))
        head_cells.append(
            _column_header_html(
                shot,
                review_reasons_by_shot.get(shot_id, []),
            )
        )
    table_parts = [
        _field_category_nav(list(_FIELD_GROUPS)),
        '<table id="shot-table" class="shot-table">',
        f"<thead><tr>{''.join(head_cells)}</tr></thead>",
    ]
    if shots:
        ordered_groups: list[dict[str, object]] = []
        rows_by_group: dict[str, list[tuple[int, Observation]]] = {}
        field_count = len(observations_by_shot[str(shots[0].get("shotID", ""))])
        for row_index in range(field_count):
            first_obs = observations_by_shot[str(shots[0].get("shotID", ""))][row_index]
            group = _group_for_field(first_obs.field)
            group_id = str(group["id"])
            if group_id not in rows_by_group:
                ordered_groups.append(group)
                rows_by_group[group_id] = []
            rows_by_group[group_id].append((row_index, first_obs))
        column_count = len(shots) + 1
        for group in ordered_groups:
            group_id = str(group["id"])
            group_rows = rows_by_group[group_id]
            collapsed = "" if bool(group.get("default_open")) else " is-collapsed"
            table_parts.append(
                f'<tbody class="field-group-tbody{collapsed}" '
                f'data-field-group="{html.escape(group_id)}">'
                '<tr class="field-group-row">'
                f'<th scope="rowgroup" colspan="{column_count}">'
                f'<button type="button" class="field-group-toggle" '
                f'data-field-group-target="{html.escape(group_id)}" '
                f'aria-expanded="{"true" if not collapsed else "false"}">'
                '<span>'
                f'<strong>{html.escape(str(group["title"]))}</strong>'
                f'<small>{html.escape(str(group["description"]))}</small>'
                '</span>'
                f'<span class="badge badge-outline">{len(group_rows)} 项</span>'
                '</button>'
                '</th></tr>'
            )
            for row_index, first_obs in group_rows:
                cells = [
                    f'<th scope="row" data-field="{html.escape(first_obs.field)}">'
                    f"{_field_label_html(first_obs.field)}</th>"
                ]
                for shot in shots:
                    obs = observations_by_shot[str(shot.get("shotID", ""))][row_index]
                    cells.append(_cell_html(obs, vocabulary.fields.get(obs.field)))
                table_parts.append(f"<tr>{''.join(cells)}</tr>")
            table_parts.append("</tbody>")
    table_parts.append("</table>")
    return "\n".join(table_parts)


def _metadata_html(status: str, shot_count: int, revision: str) -> str:
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entries = [
        ("校对状态", _document_status_label(status)),
        ("镜头数", str(shot_count)),
        ("生成时间", generated),
    ]
    items = "".join(
        f"<div><dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd></div>"
        for label, value in entries
    )
    return (
        '<header class="metadata card" id="metadata">'
        '<div class="metadata-topline"><div class="metadata-brand">'
        '<img class="brand-logo" src="assets/memoloupe-logo.png" alt="MemoLoupe" height="28">'
        "<h1>镜头拉片校对台</h1>"
        "</div>"
        '<span class="badge badge-outline">离线可打开</span></div>'
        f'<dl class="metadata-grid">{items}</dl>'
        "</header>"
    )


def _validation_html(validation_summary: str | None, warnings: list[str]) -> str:
    """校验摘要区（只读）：外部校验结果 + corrections overlay 警告。"""
    parts = [
        '<section id="validation-summary" class="card" aria-label="检查结果">',
        '<div class="card-header"><div><h2 class="card-title">检查结果</h2>'
        '<p class="card-description">页面生成和校对记录的检查状态。</p>'
        "</div></div>",
    ]
    if validation_summary is None:
        parts.append('<p class="validation-empty">未提供检查结果</p>')
    else:
        parts.append(f'<pre class="validation-report">{html.escape(validation_summary)}</pre>')
    if warnings:
        parts.append('<ul class="correction-warnings">')
        for warning in warnings:
            parts.append(f"<li>{html.escape(warning)}</li>")
        parts.append("</ul>")
    parts.append("</section>")
    return "".join(parts)


def _json_for_script(payload: object) -> str:
    """转成可安全嵌入内联 ``<script>`` 的 JSON 字面量。"""
    return (
        json.dumps(payload, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _js_string(value: str) -> str:
    """转成安全的 JS 字符串字面量。"""
    return _json_for_script(value)


def render_shot_html(
    out_dir: Path,
    *,
    status: str = "draft",
    server_mode: bool = False,
    validation_summary: str | None = None,
) -> Path:
    """渲染 out_dir 的 shot-analysis.html 并原子写入，返回写入路径。

    渲染顺序：raw → resolver → corrections overlay → HTML（docs/02 §6）。
    存在 corrections 文件时文档状态由 ``document_status`` 推导，否则回落到
    ``status`` 参数。shots.json 缺失/不可读时抛 :class:`ArtifactError`；
    其余 raw 文件缺失时对应字段落 unknown。

    - ``server_mode=True`` 时页面注入 ``window.__REVIEW_SERVER__ = true``，
      显示"保存到本地"按钮（POST /api/corrections）；
    - ``validation_summary`` 写入只读校验摘要区。
    """
    out_dir = Path(out_dir)
    if status not in DOCUMENT_STATUSES:
        raise ValueError(f"非法文档状态: {status!r}（应为 {sorted(DOCUMENT_STATUSES)}）")
    raws = _load_raws(out_dir)
    _copy_logo_asset(out_dir)
    shots_doc = raws.get("shots")
    if shots_doc is None:
        raise ArtifactError("shots", "raw/shots.json 缺失或不可读，无法渲染 shot-analysis.html")
    shot_entries = shots_doc.get("shots")
    if not isinstance(shot_entries, list) or not shot_entries:
        raise ArtifactError("shots", "raw/shots.json 不含任何镜头")
    shots = sorted(
        (s for s in shot_entries if isinstance(s, dict) and isinstance(s.get("shotID"), str)),
        key=lambda s: (s.get("finalStartMs") is None, s.get("finalStartMs", 0)),
    )
    if not shots:
        raise ArtifactError("shots", "raw/shots.json 不含合法镜头条目")

    revision = "unknown"
    media = raws.get("media")
    if media:
        value = media.get("source", {}).get("revisionID")
        if isinstance(value, str) and value:
            revision = value

    vocabulary = load_vocabulary()
    loaded = _load_corrections(out_dir)

    observations_by_shot: dict[str, list[Observation]] = {}
    review_reasons_by_shot: dict[str, list[str]] = {}
    warnings: list[str] = []
    for shot in shots:
        shot_id = str(shot["shotID"])
        observations, reasons = build_observations_with_review(
            shot_id, raws, DEFAULT_RESOLVERS
        )
        if loaded is not None:
            corrections_mod, corrections = loaded
            observations, corr_warnings = corrections_mod.apply_corrections(
                observations, corrections, revision
            )
            warnings.extend(str(w) for w in corr_warnings)
        observations_by_shot[shot_id] = observations
        review_reasons_by_shot[shot_id] = list(reasons)

    if loaded is not None:
        corrections_mod, corrections = loaded
        document_status = corrections_mod.document_status(corrections, revision)
        if document_status not in DOCUMENT_STATUSES:
            raise ArtifactError(
                "corrections",
                f"document_status 返回非法状态 {document_status!r}"
                f"（应为 {sorted(DOCUMENT_STATUSES)}）",
            )
    else:
        document_status = status

    frame_refs = _frame_refs(raws, out_dir)
    full_video_src = _full_video_src(raws, out_dir)
    document = _TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "__DOCUMENT_STATUS__": document_status,
        "__CONTRACT_VERSION__": CONTRACT_VERSION,
        "__SOURCE_REVISION__": html.escape(revision),
        "__SHOT_RENDER_VERSION__": SHOT_RENDER_VERSION,
        "__SOURCE_REVISION_JS__": _js_string(revision),
        "__SERVER_MODE__": "true" if server_mode else "false",
        "__FULL_VIDEO_SRC_JS__": (
            "null" if full_video_src is None else _js_string(full_video_src)
        ),
        "__REVIEW_REASONS_JSON__": _json_for_script(review_reasons_by_shot),
        "__SHOT_INSPECTOR_JSON__": _shot_inspector_json(
            shots,
            observations_by_shot,
            review_reasons_by_shot,
            frame_refs,
            out_dir,
            raws,
        ),
        "<!--METADATA-->": _metadata_html(document_status, len(shots), revision),
        "<!--VALIDATION_SUMMARY-->": _validation_html(validation_summary, warnings),
        "<!--SHOT_SUMMARY-->": _summary_html(
            document_status, shots, review_reasons_by_shot, raws
        ),
        "<!--SHOT_TIMELINE-->": _timeline_html(
            shots, review_reasons_by_shot, frame_refs, out_dir, raws
        ),
        "<!--SHOT_TABLE-->": _table_html(
            shots,
            observations_by_shot,
            review_reasons_by_shot,
            vocabulary,
        ),
    }
    for placeholder, content in replacements.items():
        if placeholder not in document:
            raise ArtifactError(
                "shot-analysis.html", f"模板缺少占位符 {placeholder!r}"
            )
        document = document.replace(placeholder, content)

    target = out_dir / "shot-analysis.html"
    write_text_atomic(target, document)
    return target
