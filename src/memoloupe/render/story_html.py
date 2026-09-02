"""render.story_html — story-analysis.html 渲染器（docs/04 §2/§4/§5/§6、roadmap 03-04）。

流程固定为：读 output-dir 的 ``raw/story-blocks.json``（必需）与
``raw/shots.json``（镜头跳转/clip 引用，缺失容忍）→ 把叙事字段与 slot
字段构造为五态 Observation（state=value/unknown、source=textModel、
evidenceRefs 指向 ``raw/story-blocks.json``）→ 应用 corrections overlay
（:mod:`memoloupe.render.corrections`，entityID=storyBlockID/slotID）→
映射到 ``templates/story-analysis.html`` 骨架 → 原子写入。

页面语义（docs/04 §4）：

- 时间线：block 按时长比例排布，可点击跳到对应 block；
- 镜头覆盖：镜头 → block 归属表，带镜头播放按钮（相对 clip 路径）；
- story-block DOM：``<section class="story-block" data-story-block-id data-shot-ids
  data-start-ms data-end-ms>``，只出现在 storyAnalysis 文档；
- 叙事字段单元格复用 Phase 1 的五态/confidence/evidenceRefs/verified 语义；
  scaffold 占位（枚举 unknown、自由文本空串）统一呈 unknown 状态；
- 受控词表字段渲染 ``<select>`` 内联编辑控件，多值/自由文本渲染 ``<input>``；
- 所有模型文本经 ``html.escape`` 后才进入 HTML；JS 注入 JSON 转义 ``</``；
- 媒体一律相对路径；缺 clips/SHxxxx.mp4 时播放按钮禁用。

模板替换完成后若仍有占位符残留，抛 :class:`ArtifactError`。
"""

from __future__ import annotations

import html
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path

from memoloupe.analysis.observations import Confidence, Observation, Source, ValueState
from memoloupe.analysis.story_prompts import (
    AUDIENCE_REACTIONS,
    DIVISION_AXES,
    INFORMATION_ROLES,
    NARRATIVE_DENSITIES,
    PRIMARY_ROLES,
    VISUAL_INDEPENDENCES,
)
from memoloupe.core.atomic_io import read_json, write_text_atomic
from memoloupe.core.errors import ArtifactError, ContractError
from memoloupe.validate.html_contract import DOCUMENT_STATUSES

STORY_RENDER_VERSION = "story-render.v1"
CONTRACT_VERSION = "1.0"
DOCUMENT_TYPE = "storyAnalysis"

_TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "templates" / "story-analysis.html"

#: 单值受控词表字段 -> 可选值（select 控件；unknown 单独追加）。
_SINGLE_ENUM_FIELDS: dict[str, tuple[str, ...]] = {
    "divisionAxis": DIVISION_AXES,
    "primaryRole": PRIMARY_ROLES,
    "narrativeDensity": NARRATIVE_DENSITIES,
    "audienceReaction": AUDIENCE_REACTIONS,
    "visualIndependence": VISUAL_INDEPENDENCES,
}

#: 多值受控词表字段（顿号分隔，input 控件，placeholder 提示词表）。
_MULTI_ENUM_FIELDS: dict[str, tuple[str, ...]] = {
    "informationRole": INFORMATION_ROLES,
}

#: block 叙事字段展示顺序；可选字段（blockTitle/boundaryBasis）由渲染器
#: 在字段存在时追加，不进此表。
_NARRATIVE_FIELDS: tuple[str, ...] = (
    "divisionAxis",
    "divisionRationale",
    "primaryRole",
    "coreContent",
    "informationRole",
    "narrativeDensity",
    "audienceReaction",
    "visualIndependence",
    "blockRelation",
    "relationReason",
)

#: slot 字段（entityID=slotID）。
_SLOT_FIELDS: tuple[str, ...] = ("slotType", "slotTitle", "slotRationale")

_FIELD_LABELS: dict[str, str] = {
    "blockTitle": "故事段标题",
    "divisionAxis": "分段依据",
    "divisionRationale": "为什么这样分段",
    "primaryRole": "叙事作用",
    "coreContent": "核心内容",
    "informationRole": "信息作用",
    "narrativeDensity": "叙事密度",
    "audienceReaction": "观众感受",
    "visualIndependence": "静音可读性",
    "blockRelation": "与前后关系",
    "relationReason": "关系说明",
    "boundaryBasis": "边界依据",
    "slotType": "结构类型",
    "slotTitle": "结构标题",
    "slotRationale": "结构说明",
}

_DOCUMENT_STATUS_LABELS = {
    "draft": "未校对",
    "underReview": "校对中",
    "confirmed": "已确认",
    "outdated": "需更新",
}

_STORY_STATUS_LABELS = {
    "complete": "已完成",
    "scaffold": "骨架已生成",
    "partial": "部分完成",
    "failed": "生成失败",
}


def _timecode(ms: int) -> str:
    total_s, rem = divmod(int(ms), 1000)
    hours, rem_s = divmod(total_s, 3600)
    minutes, seconds = divmod(rem_s, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{rem:03d}"
    return f"{minutes:02d}:{seconds:02d}.{rem:03d}"


def _js_string(value: str) -> str:
    """转成安全的 JS 字符串字面量（json.dumps + 转义 "</"，防 </script> 逃逸）。"""
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def _load_raws(out_dir: Path) -> tuple[dict, dict | None, dict | None]:
    """加载 story-blocks（必需）+ shots/media（可选）。"""
    try:
        story = read_json(out_dir / "raw" / "story-blocks.json")
    except ContractError as exc:
        raise ArtifactError(
            "story-blocks", f"raw/story-blocks.json 缺失或不可读：{exc}"
        ) from None
    raws: dict[str, dict | None] = {}
    for name in ("shots", "media"):
        try:
            raws[name] = read_json(out_dir / "raw" / f"{name}.json")
        except ContractError:
            raws[name] = None
    return story, raws["shots"], raws["media"]


def _load_optional_raws(out_dir: Path) -> dict[str, dict | None]:
    """加载用于 story 叠加展示的 Phase 1 证据；缺失时静默降级。"""
    raws: dict[str, dict | None] = {}
    for name in ("unified-media", "frame-evidence", "music-flags", "audio-energy"):
        try:
            raws[name] = read_json(out_dir / "raw" / f"{name}.json")
        except ContractError:
            raws[name] = None
    return raws


def _field_observation(
    entity_id: str,
    field: str,
    value: object,
    index: int,
    *,
    collection: str,
) -> Observation:
    """把 block/slot 的一个叙事字段构造为五态 Observation。

    - 非空且非 ``unknown`` 的文本为 ``value``（source=textModel）；
    - 空串或 ``unknown`` 占位为 ``unknown``（scaffold 语义，不伪造）；
    - evidenceRefs 指向 ``raw/story-blocks.json``（值可追溯）。
    """
    text = str(value).strip() if value is not None else ""
    is_value = bool(text) and text != "unknown"
    return Observation(
        field=field,
        shot_id=entity_id,  # 通用实体位：storyBlockID 或 slotID
        value=text if is_value else None,
        state=ValueState.VALUE if is_value else ValueState.UNKNOWN,
        confidence=Confidence.HIGH if is_value else Confidence.UNKNOWN,
        evidence_refs=(f"raw/story-blocks.json#{collection}[{index}].{field}",),
        source=Source.TEXT_MODEL,
        verified=False,
    )


def _block_observations(block: dict, index: int) -> list[Observation]:
    block_id = str(block["storyBlockID"])
    obs = [
        _field_observation(block_id, field, block.get(field), index, collection="blocks")
        for field in _NARRATIVE_FIELDS
    ]
    for optional in ("blockTitle", "boundaryBasis"):
        if optional in block:
            obs.append(
                _field_observation(
                    block_id, optional, block.get(optional), index, collection="blocks"
                )
            )
    return obs


def _slot_observations(slot: dict, index: int) -> list[Observation]:
    slot_id = str(slot["slotID"])
    return [
        _field_observation(slot_id, field, slot.get(field), index, collection="slots")
        for field in _SLOT_FIELDS
    ]


def _state_visible_text(obs: Observation) -> str:
    if obs.state == ValueState.VALUE:
        return html.escape(str(obs.value))
    return "待确认"


def _field_label(field: str) -> str:
    return _FIELD_LABELS.get(field, field)


def _document_status_label(status: str) -> str:
    return _DOCUMENT_STATUS_LABELS.get(status, status)


def _story_status_label(status: object) -> str:
    return _STORY_STATUS_LABELS.get(str(status), str(status))


def _confidence_visible_text(value: str) -> str:
    return {
        "high": "可信度高",
        "medium": "可信度中",
        "low": "可信度低",
        "unknown": "可信度待确认",
    }.get(value, "可信度待确认")


def _initial_edit_value(obs: Observation) -> str:
    if obs.state == ValueState.VALUE and obs.value is not None:
        return str(obs.value)
    return ""


def _edit_control_html(obs: Observation) -> str:
    """内联编辑控件：单值受控词表 <select>，多值/自由文本 <input>。"""
    esc_field = html.escape(obs.field)
    esc_entity = html.escape(obs.shot_id)
    aria = html.escape(f"编辑 {obs.shot_id} {obs.field}")
    initial = html.escape(_initial_edit_value(obs))
    single = _SINGLE_ENUM_FIELDS.get(obs.field)
    if single is not None:
        options = list(single)
        if "unknown" not in options:
            options.append("unknown")
        current = "unknown" if obs.state == ValueState.UNKNOWN else str(obs.value or "")
        parts: list[str] = []
        for opt in options:
            selected = " selected" if current == opt else ""
            label = "待确认" if opt == "unknown" else opt
            parts.append(
                f'<option value="{html.escape(opt)}"{selected}>{html.escape(label)}</option>'
            )
        return (
            f'<select class="cell-edit" data-field="{esc_field}" '
            f'data-entity-id="{esc_entity}" data-initial-value="{initial}" '
            f'aria-label="{aria}">{"".join(parts)}</select>'
        )
    placeholder = ""
    multi = _MULTI_ENUM_FIELDS.get(obs.field)
    if multi is not None:
        placeholder = html.escape(
            f" 词表：{'、'.join(multi)}（多选顿号分隔）"
        )
    return (
        f'<input type="text" class="cell-edit" data-field="{esc_field}" '
        f'data-entity-id="{esc_entity}" data-initial-value="{initial}" '
        f'value="{initial}" placeholder="{placeholder}" aria-label="{aria}">'
    )


def _cell_html(obs: Observation, *, slot: bool) -> str:
    """叙事字段单元格：五态属性 + 可见文本 + 编辑控件 + 核实开关。

    ``slot=True`` 时用 ``data-slot-id``，否则用 ``data-block-id``。
    """
    entity_attr = "data-slot-id" if slot else "data-block-id"
    attrs = (
        f'data-field="{html.escape(obs.field)}" '
        f'{entity_attr}="{html.escape(obs.shot_id)}" '
        f'data-entity-id="{html.escape(obs.shot_id)}" '
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
    esc_entity = html.escape(obs.shot_id)
    parts = [f'<span class="cell-text {state_class}">{_state_visible_text(obs)}</span>']
    parts.append(
        f'<span class="cell-confidence">{html.escape(_confidence_visible_text(obs.confidence.value))}</span>'
    )
    if obs.verified:
        parts.append('<span class="cell-verified">已核实</span>')
    parts.append(_edit_control_html(obs))
    checked = " checked" if obs.verified else ""
    parts.append(
        f'<label class="cell-verify"><input type="checkbox" class="verify-toggle" '
        f'data-field="{esc_field}" data-entity-id="{esc_entity}" '
        f'aria-label="核实 {esc_entity} {esc_field}"{checked}> 已核实</label>'
    )
    return f"<td {attrs}>{''.join(parts)}</td>"


def _clip_srcs(out_dir: Path, shot_ids: list[str]) -> dict[str, str]:
    """shotID -> 存在的 clip 相对路径（缺失不引用，播放按钮禁用）。"""
    return {
        sid: f"clips/{sid}.mp4"
        for sid in shot_ids
        if (out_dir / "clips" / f"{sid}.mp4").is_file()
    }


def _frame_refs(raws: dict[str, dict | None]) -> dict[str, str]:
    """shotID -> 代表帧相对路径。"""
    doc = raws.get("frame-evidence")
    frames = doc.get("frames") if isinstance(doc, dict) else None
    result: dict[str, str] = {}
    if not isinstance(frames, list):
        return result
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        if frame.get("frameType") != "representative":
            continue
        shot_id = frame.get("shotID")
        file_ref = frame.get("fileRef")
        if isinstance(shot_id, str) and isinstance(file_ref, str) and file_ref:
            result.setdefault(shot_id, file_ref)
    return result


def _unified_shots(raws: dict[str, dict | None]) -> dict[str, dict]:
    """shotID -> UnifiedMLLM 结构化镜头摘要。"""
    doc = raws.get("unified-media")
    batches = doc.get("batches") if isinstance(doc, dict) else None
    result: dict[str, dict] = {}
    if not isinstance(batches, list):
        return result
    for batch in batches:
        if not isinstance(batch, dict):
            continue
        response = batch.get("response")
        shots = response.get("shots") if isinstance(response, dict) else None
        if not isinstance(shots, list):
            continue
        for shot in shots:
            if isinstance(shot, dict) and isinstance(shot.get("shotID"), str):
                result[shot["shotID"]] = shot
    return result


def _music_by_shot(raws: dict[str, dict | None]) -> dict[str, str]:
    doc = raws.get("music-flags")
    shots = doc.get("shots") if isinstance(doc, dict) else None
    result: dict[str, str] = {}
    if not isinstance(shots, list):
        return result
    for shot in shots:
        if isinstance(shot, dict) and isinstance(shot.get("shotID"), str):
            state = shot.get("state")
            if isinstance(state, str):
                result[shot["shotID"]] = state
    return result


def _energy_by_shot(raws: dict[str, dict | None]) -> dict[str, str]:
    doc = raws.get("audio-energy")
    shots = doc.get("shots") if isinstance(doc, dict) else None
    result: dict[str, str] = {}
    if not isinstance(shots, list):
        return result
    for shot in shots:
        if isinstance(shot, dict) and isinstance(shot.get("shotID"), str):
            label = shot.get("label")
            if isinstance(label, str):
                result[shot["shotID"]] = label
    return result


def _shot_summary_text(model_shot: dict | None) -> str:
    if not isinstance(model_shot, dict):
        return "该镜头暂无视频理解摘要。"
    visual = model_shot.get("visual")
    visual = visual if isinstance(visual, dict) else {}
    for key in ("contentSummary", "subjects", "actions", "setting"):
        value = visual.get(key)
        if isinstance(value, str) and value.strip() and value.strip() != "无":
            return value.strip()
    return "该镜头暂无视频理解摘要。"


def _shot_meta_badges(
    shot_id: str,
    model_shot: dict | None,
    music_by_shot: dict[str, str],
    energy_by_shot: dict[str, str],
) -> list[str]:
    badges: list[str] = []
    if isinstance(model_shot, dict):
        visual = model_shot.get("visual")
        audio = model_shot.get("audio")
        function = model_shot.get("function")
        if isinstance(visual, dict):
            for key in ("framing", "cameraMovement"):
                value = visual.get(key)
                if isinstance(value, str) and value.strip() and value.strip() != "无":
                    badges.append(value.strip())
        if isinstance(function, dict):
            value = function.get("shotTone")
            if isinstance(value, str) and value.strip() and value.strip() != "无":
                badges.append(value.strip())
        if isinstance(audio, dict):
            value = audio.get("bgmStyle")
            if isinstance(value, str) and value.strip() and value.strip() != "无":
                badges.append(value.strip())
    music = music_by_shot.get(shot_id)
    if music == "music":
        badges.append("有背景音乐")
    elif music == "silent":
        badges.append("音乐静默")
    elif music == "unknown":
        badges.append("声音待确认")
    energy = energy_by_shot.get(shot_id)
    if energy:
        badges.append(f"音量{energy}")
    return badges[:6]


def _story_overview_html(story: dict, blocks: list[dict], shots: list[dict]) -> str:
    slots = story.get("slots", [])
    first_title = ""
    if blocks:
        title = blocks[0].get("blockTitle")
        if isinstance(title, str):
            first_title = title.strip()
    density = ""
    if blocks:
        value = blocks[0].get("narrativeDensity")
        if isinstance(value, str) and value != "unknown":
            density = value
    cards = [
        ("故事状态", _story_status_label(story.get("status", "")), "模型结果"),
        ("故事段", str(len(blocks)), "叙事分组"),
        ("结构槽", str(len(slots) if isinstance(slots, list) else 0), "上层结构"),
        ("镜头证据", str(len(shots)), "来自镜头分析"),
    ]
    if density:
        cards.append(("叙事密度", density, "整体节奏"))
    parts = [
        '<section id="story-overview" class="card story-overview" aria-label="故事分析总览">',
        '<div class="card-header"><div>',
        '<p class="eyebrow">Story Layer</p>',
        '<h2 class="card-title">故事层叠加在镜头拉片之上</h2>',
        '<p class="card-description">这一页不会替代镜头分析，而是在每个故事段下继续保留镜头、代表帧、片段播放和镜头级摘要。</p>',
        '</div><a class="link-button" href="shot-analysis.html">回到镜头审片页</a></div>',
    ]
    if first_title:
        parts.append(f'<p class="hero-story-title">{html.escape(first_title)}</p>')
    parts.append('<div class="summary-grid">')
    for label, value, note in cards:
        parts.append(
            '<article class="metric-card">'
            f'<p class="metric-label">{html.escape(label)}</p>'
            f'<p class="metric-value">{html.escape(value)}</p>'
            f'<span class="badge badge-outline">{html.escape(note)}</span>'
            '</article>'
        )
    parts.append("</div></section>")
    return "".join(parts)


def _block_shot_layer_html(
    block: dict,
    shots_by_id: dict[str, dict],
    clip_srcs: dict[str, str],
    frame_refs: dict[str, str],
    unified_by_shot: dict[str, dict],
    music_by_shot: dict[str, str],
    energy_by_shot: dict[str, str],
) -> str:
    shot_ids = [str(s) for s in block.get("shotIDs", [])]
    if not shot_ids:
        return ""
    parts = [
        '<div class="block-shot-layer" aria-label="故事段包含的镜头证据">',
        '<div class="subsection-heading"><div>',
        '<h4>镜头证据层</h4>',
        '<p>沿用上一阶段的镜头、缩略图、片段和视频理解摘要。</p>',
        '</div></div>',
        '<div class="story-shot-grid">',
    ]
    for shot_id in shot_ids:
        shot = shots_by_id.get(shot_id, {})
        start_ms = shot.get("finalStartMs")
        end_ms = shot.get("finalEndMs")
        time_text = ""
        if isinstance(start_ms, int) and isinstance(end_ms, int):
            time_text = f"{_timecode(start_ms)} – {_timecode(end_ms)}"
        model_shot = unified_by_shot.get(shot_id)
        summary = _shot_summary_text(model_shot)
        badges = _shot_meta_badges(shot_id, model_shot, music_by_shot, energy_by_shot)
        clip = clip_srcs.get(shot_id)
        frame_ref = frame_refs.get(shot_id)
        parts.append(f'<article class="story-shot-card" data-shot-id="{html.escape(shot_id)}">')
        if frame_ref:
            parts.append(
                f'<img class="story-shot-thumb" src="{html.escape(frame_ref)}" '
                f'alt="{html.escape(shot_id)} 代表帧" loading="lazy">'
            )
        else:
            parts.append('<div class="story-shot-thumb is-empty">无代表帧</div>')
        parts.append('<div class="story-shot-body">')
        parts.append(
            f'<div class="story-shot-topline"><strong>{html.escape(shot_id)}</strong>'
            f'<span>{html.escape(time_text)}</span></div>'
        )
        parts.append(f'<p class="story-shot-summary">{html.escape(summary)}</p>')
        if badges:
            parts.append('<div class="shot-badges">')
            for badge in badges:
                parts.append(f'<span class="badge badge-soft">{html.escape(badge)}</span>')
            parts.append("</div>")
        if clip is not None and isinstance(start_ms, int) and isinstance(end_ms, int):
            parts.append(
                f'<button type="button" class="jump-btn shot-jump" '
                f'data-clip-src="{html.escape(clip)}" data-start-ms="{start_ms}" '
                f'data-end-ms="{end_ms}" aria-label="播放镜头 {html.escape(shot_id)}">'
                "播放片段</button>"
            )
        else:
            parts.append(
                f'<button type="button" class="jump-btn shot-jump" disabled '
                f'aria-label="{html.escape(shot_id)} 无法播放">暂无片段</button>'
            )
        parts.append("</div></article>")
    parts.append("</div></div>")
    return "".join(parts)


def _block_html(
    block: dict,
    index: int,
    observations: list[Observation],
    clip_srcs: dict[str, str],
    shots_by_id: dict[str, dict],
    frame_refs: dict[str, str],
    unified_by_shot: dict[str, dict],
    music_by_shot: dict[str, str],
    energy_by_shot: dict[str, str],
    document_status: str,
) -> str:
    block_id = str(block["storyBlockID"])
    shot_ids = [str(s) for s in block.get("shotIDs", [])]
    start_ms, end_ms = block.get("startMs"), block.get("endMs")
    boundary = block.get("boundary", {})
    boundary = boundary if isinstance(boundary, dict) else {}
    esc_id = html.escape(block_id)
    lines = [
        f'<section class="story-block" data-story-block-id="{esc_id}" '
        f'data-shot-ids="{html.escape(" ".join(shot_ids))}" '
        f'data-start-ms="{start_ms}" data-end-ms="{end_ms}" id="{esc_id}">',
        '<header class="block-header">',
        '<div>',
        f'<p class="eyebrow">故事段 {esc_id}</p>',
        "<h3>",
    ]
    title = block.get("blockTitle")
    if isinstance(title, str) and title.strip():
        lines.append(f'<span class="block-title">{html.escape(title.strip())}</span>')
    else:
        lines.append("未命名故事段")
    lines.append("</h3>")
    lines.append("</div>")
    if isinstance(start_ms, int) and isinstance(end_ms, int):
        lines.append('<div class="block-meta">')
        lines.append(
            f'<span class="timecode">{_timecode(start_ms)} – {_timecode(end_ms)}</span>'
        )
        lines.append(f'<span class="duration">{max(0, end_ms - start_ms) / 1000:.1f}s</span>')
        lines.append("</div>")
    lines.append(
        f'<span class="boundary-badge" data-level="{html.escape(str(boundary.get("level", "")))}" '
        f'data-signal="{html.escape(str(boundary.get("signal", "")))}">'
        f'{html.escape(str(boundary.get("label", "")))}</span>'
    )
    lines.append(f'<span class="status-badge status-{html.escape(document_status)}">'
                 f"{html.escape(_document_status_label(document_status))}</span>")
    lines.append("</header>")
    core = block.get("coreContent")
    if isinstance(core, str) and core.strip():
        lines.append(f'<p class="block-core">{html.escape(core.strip())}</p>')
    if shot_ids:
        lines.append(f'<p class="block-shots">{len(shot_ids)} 个镜头共同构成这一故事段。</p>')
        lines.append(
            _block_shot_layer_html(
                block,
                shots_by_id,
                clip_srcs,
                frame_refs,
                unified_by_shot,
                music_by_shot,
                energy_by_shot,
            )
        )
    if observations:
        lines.append('<details class="narrative-details" open><summary>详细叙事字段</summary>')
        lines.append("<table class=\"narrative\"><tbody>")
        for obs in observations:
            esc_field = html.escape(obs.field)
            label = html.escape(_field_label(obs.field))
            lines.append(
                f'<tr><th scope="row" data-field="{esc_field}">{label}</th>'
                f"{_cell_html(obs, slot=False)}</tr>"
            )
        lines.append("</tbody></table></details>")
    relation = block.get("blockRelation")
    reason = block.get("relationReason")
    if isinstance(relation, str) and relation:
        lines.append('<div class="block-relations">')
        lines.append(f"<div>关系：{html.escape(relation)}</div>")
        if isinstance(reason, str) and reason:
            lines.append(f"<div>理由：{html.escape(reason)}</div>")
        lines.append("</div>")
    lines.append("</section>")
    return "".join(lines)


def _slots_html(
    slots: list[dict],
    observations_by_slot: dict[str, list[Observation]],
    block_ids: set[str],
) -> str:
    if not slots:
        return (
            '<section class="slots-card card"><h2>故事结构</h2>'
            '<p class="validation-empty">尚未生成结构槽；当前只有确定性的故事骨架。</p>'
            "</section>"
        )
    lines = ['<section class="slots-card card"><h2>故事结构</h2>']
    for index, slot in enumerate(slots):
        slot_id = str(slot["slotID"])
        esc_id = html.escape(slot_id)
        lines.append(f'<div class="story-slot" data-slot-id="{esc_id}" id="{esc_id}">')
        lines.append('<header><div><p class="eyebrow">结构槽</p><h3>')
        lines.append(
            f'{html.escape(str(slot.get("slotTitle") or slot_id))}'
        )
        lines.append("</h3></div>")
        slot_type = slot.get("slotType")
        if isinstance(slot_type, str) and slot_type.strip():
            lines.append(f'<span class="slot-type">{html.escape(slot_type.strip())}</span>')
        lines.append("</header>")
        refs = [
            f'<a href="#{html.escape(bid)}">{html.escape(bid)}</a>'
            for bid in slot.get("blockIDs", [])
            if bid in block_ids
        ]
        if refs:
            lines.append(f'<div class="slot-blocks">包含故事段：{"、".join(refs)}</div>')
        rationale = slot.get("slotRationale")
        if isinstance(rationale, str) and rationale:
            lines.append(f'<p class="slot-rationale">{html.escape(rationale)}</p>')
        obs = observations_by_slot.get(slot_id)
        if obs:
            lines.append('<table class="slot-fields"><tbody>')
            for entry in obs:
                esc_field = html.escape(entry.field)
                label = html.escape(_field_label(entry.field))
                lines.append(
                    f'<tr><th scope="row" data-field="{esc_field}">{label}</th>'
                    f"{_cell_html(entry, slot=True)}</tr>"
                )
            lines.append("</tbody></table>")
        lines.append("</div>")
    lines.append("</section>")
    return "".join(lines)


def _timeline_html(blocks: list[dict]) -> str:
    if not blocks:
        return ""
    starts = [int(b["startMs"]) for b in blocks if isinstance(b.get("startMs"), int)]
    ends = [int(b["endMs"]) for b in blocks if isinstance(b.get("endMs"), int)]
    if not starts or not ends:
        return ""
    total = max(0, max(ends) - min(starts)) or 1
    parts = [
        '<section class="timeline-card card"><div class="card-header"><div>',
        '<h2 class="card-title">叙事时间线</h2>',
        '<p class="card-description">上层是故事段，点击可跳到对应段落；下层镜头证据仍保留在每个故事段中。</p>',
        '</div><span class="badge badge-outline">按故事段</span></div>',
        '<div class="story-timeline" aria-label="故事块时间线">',
    ]
    for block in blocks:
        start = int(block["startMs"])
        end = int(block["endMs"])
        width = max(4.0, (end - start) / total * 100.0)
        esc_id = html.escape(str(block["storyBlockID"]))
        parts.append(
            f'<div class="story-timeline-segment" style="width:{width:.2f}%;" '
            f'data-story-block-id="{esc_id}"><a href="#{esc_id}">{html.escape(str(block.get("blockTitle") or esc_id))}</a></div>'
        )
    parts.append("</div></section>")
    return "".join(parts)


def _coverage_html(blocks: list[dict], shots: list[dict], clip_srcs: dict[str, str]) -> str:
    """镜头覆盖表：镜头 → 所属 block，带播放按钮。"""
    block_by_shot: dict[str, tuple[str, str, int, int]] = {}
    for block in blocks:
        block_id = str(block["storyBlockID"])
        title = block.get("blockTitle")
        title = str(title).strip() if isinstance(title, str) and title.strip() else ""
        start, end = block.get("startMs"), block.get("endMs")
        for sid in block.get("shotIDs", []):
            if isinstance(sid, str) and sid not in block_by_shot:
                block_by_shot[sid] = (block_id, title, start, end)
    if not shots:
        return ""
    lines = [
        '<details class="coverage-card card"><summary>镜头覆盖明细</summary>',
        '<table class="coverage-table">',
        "<thead><tr><th scope=\"col\">镜头</th><th scope=\"col\">时间</th>"
        "<th scope=\"col\">所属故事块</th><th scope=\"col\">播放</th></tr></thead>",
        "<tbody>",
    ]
    for shot in shots:
        sid = str(shot.get("shotID", ""))
        esc_id = html.escape(sid)
        start, end = shot.get("finalStartMs"), shot.get("finalEndMs")
        time_text = ""
        if isinstance(start, int) and isinstance(end, int):
            time_text = f"{_timecode(start)} – {_timecode(end)}"
        block_ref = ""
        if sid in block_by_shot:
            block_id, title, b_start, b_end = block_by_shot[sid]
            block_ref = f'<a href="#{html.escape(block_id)}">{html.escape(block_id)}</a>'
            if title:
                block_ref += f"（{html.escape(title)}）"
        clip = clip_srcs.get(sid)
        if clip is not None and isinstance(start, int) and isinstance(end, int):
            play = (
                f'<button type="button" class="jump-btn shot-jump" data-clip-src="{html.escape(clip)}" '
                f'data-start-ms="{start}" data-end-ms="{end}" '
                f'aria-label="播放镜头 {esc_id}">播放</button>'
            )
        else:
            play = (
                f'<button type="button" class="jump-btn shot-jump" disabled '
                f'aria-label="{esc_id} 无法播放">暂无片段</button>'
            )
        lines.append(
            f"<tr><td>{esc_id}</td><td>{html.escape(time_text)}</td>"
            f"<td>{block_ref}</td><td>{play}</td></tr>"
        )
    lines.append("</tbody></table></details>")
    return "".join(lines)


def _metadata_html(
    status: str,
    story: dict,
    shot_count: int,
    revision: str,
) -> str:
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    blocks = story.get("blocks", [])
    slots = story.get("slots", [])
    entries = [
        ("校对状态", _document_status_label(status)),
        ("故事块数", str(len(blocks))),
        ("结构槽数", str(len(slots))),
        ("镜头数", str(shot_count)),
        ("生成时间", generated),
    ]
    items = "".join(
        f"<div><dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd></div>"
        for label, value in entries
    )
    return (
        '<header class="metadata" id="metadata">'
        '<p class="eyebrow">MemoLoupe 故事工作台</p>'
        "<h1>故事拉片校对台</h1>"
        f'<dl class="metadata-grid">{items}</dl>'
        "</header>"
    )


def _validation_html(validation_summary: str | None, warnings: list[str]) -> str:
    parts = [
        '<section id="validation-summary" aria-label="检查结果">',
        "<h2>检查结果</h2>",
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


def render_story_html(
    out_dir: Path,
    *,
    status: str = "draft",
    server_mode: bool = False,
    validation_summary: str | None = None,
) -> Path:
    """渲染 out_dir 的 story-analysis.html 并原子写入，返回写入路径。

    渲染顺序：raw → 五态观察 → corrections overlay → HTML（docs/02 §6）。
    存在 corrections 文件时文档状态由 ``document_status`` 推导，否则回落到
    ``status`` 参数。``raw/story-blocks.json`` 缺失/不可读时抛
    :class:`ArtifactError`；``raw/shots.json`` 缺失时镜头覆盖表与播放按钮
    降级为空/禁用。
    """
    out_dir = Path(out_dir)
    if status not in DOCUMENT_STATUSES:
        raise ValueError(f"非法文档状态: {status!r}（应为 {sorted(DOCUMENT_STATUSES)}）")
    story, shots_doc, media = _load_raws(out_dir)
    phase1_raws = _load_optional_raws(out_dir)

    shots: list[dict] = []
    shot_entries = shots_doc.get("shots") if isinstance(shots_doc, dict) else None
    if isinstance(shot_entries, list):
        shots = sorted(
            (s for s in shot_entries if isinstance(s, dict) and isinstance(s.get("shotID"), str)),
            key=lambda s: (s.get("finalStartMs") is None, s.get("finalStartMs", 0)),
        )

    revision = "unknown"
    if media:
        value = media.get("source", {}).get("revisionID")
        if isinstance(value, str) and value:
            revision = value

    loaded = _load_corrections(out_dir)
    warnings: list[str] = []
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

    # 五态观察 + corrections overlay（entityID = storyBlockID / slotID）。
    observations_by_block: dict[str, list[Observation]] = {}
    observations_by_slot: dict[str, list[Observation]] = {}
    blocks = [b for b in story.get("blocks", []) if isinstance(b, dict)]
    for index, block in enumerate(blocks):
        observations = _block_observations(block, index)
        if loaded is not None:
            corrections_mod, corrections = loaded
            observations, corr_warnings = corrections_mod.apply_corrections(
                observations, corrections, revision
            )
            warnings.extend(str(w) for w in corr_warnings)
        observations_by_block[str(block["storyBlockID"])] = observations
    slots = [s for s in story.get("slots", []) if isinstance(s, dict)]
    for index, slot in enumerate(slots):
        observations = _slot_observations(slot, index)
        if loaded is not None:
            corrections_mod, corrections = loaded
            observations, corr_warnings = corrections_mod.apply_corrections(
                observations, corrections, revision
            )
            warnings.extend(str(w) for w in corr_warnings)
        observations_by_slot[str(slot["slotID"])] = observations

    shot_ids = [str(s["shotID"]) for s in shots]
    clip_srcs = _clip_srcs(out_dir, shot_ids)
    block_ids = {str(b["storyBlockID"]) for b in blocks}
    shots_by_id = {str(s["shotID"]): s for s in shots if isinstance(s.get("shotID"), str)}
    frame_refs = _frame_refs(phase1_raws)
    unified_by_shot = _unified_shots(phase1_raws)
    music_by_shot = _music_by_shot(phase1_raws)
    energy_by_shot = _energy_by_shot(phase1_raws)

    document = _TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "__DOCUMENT_STATUS__": document_status,
        "__CONTRACT_VERSION__": CONTRACT_VERSION,
        "__SOURCE_REVISION__": html.escape(revision),
        "__SOURCE_REVISION_JS__": _js_string(revision),
        "__SERVER_MODE__": "true" if server_mode else "false",
        "<!--METADATA-->": _metadata_html(document_status, story, len(shots), revision),
        "<!--VALIDATION_SUMMARY-->": _validation_html(validation_summary, warnings),
        "<!--STORY_OVERVIEW-->": _story_overview_html(story, blocks, shots),
        "<!--STORY_TIMELINE-->": _timeline_html(blocks),
        "<!--SHOT_COVERAGE-->": _coverage_html(blocks, shots, clip_srcs),
        "<!--STORY_BLOCKS-->": "".join(
            _block_html(
                block, index,
                observations_by_block[str(block["storyBlockID"])],
                clip_srcs,
                shots_by_id,
                frame_refs,
                unified_by_shot,
                music_by_shot,
                energy_by_shot,
                document_status,
            )
            for index, block in enumerate(blocks)
        ),
        "<!--STORY_SLOTS-->": _slots_html(slots, observations_by_slot, block_ids),
    }
    for placeholder, content in replacements.items():
        if placeholder not in document:
            raise ArtifactError(
                "story-analysis.html", f"模板缺少占位符 {placeholder!r}"
            )
        document = document.replace(placeholder, content)

    target = out_dir / "story-analysis.html"
    write_text_atomic(target, document)
    return target


def _load_corrections(out_dir: Path):
    """加载 corrections overlay，返回 ``(module, Corrections)`` 或 None。

    corrections 文件不存在时返回 None；``render.corrections`` 模块尚不可用时
    同样返回 None——显式降级为无 overlay 渲染。
    """
    corr_path = out_dir / "corrections" / f"{DOCUMENT_TYPE}.json"
    if not corr_path.is_file():
        return None
    try:
        corrections_mod = importlib.import_module("memoloupe.render.corrections")
    except ImportError:
        return None
    return corrections_mod, corrections_mod.load_corrections(out_dir, DOCUMENT_TYPE)
