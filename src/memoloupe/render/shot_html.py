"""render.shot_html — shot-analysis.html 渲染器（docs/04 §2/§3/§5/§6）。

流程固定为：读 output-dir 的 raw/*.json（缺失容忍为 None）→ 经
:func:`memoloupe.analysis.resolvers.build_observations_with_review` 生成
Observation 并收集 review 理由 → 应用 corrections overlay
（:mod:`memoloupe.render.corrections`，docs/02 §6 渲染顺序）→ 映射到
``templates/shot-analysis.html`` 骨架（占位符字符串替换）→ 原子写入。

- 所有模型/检测原文经 ``html.escape`` 后才进入 HTML；
- CSS/JS 由模板固定内联，绝不动态拼接用户内容（CSP 例外因此安全）；
- 注入 JS 的 JSON 数据经 ``json.dumps`` 并转义 ``</``，防止 ``</script>`` 逃逸；
- 媒体一律相对路径；缺 clips/SHxxxx.mp4 时播放按钮禁用；
- 受控词表字段渲染 ``<select>`` 内联编辑控件，自由文本字段渲染 ``<input>``；
- 命中人工修正的单元格带 ``data-source="human"`` 与 ``data-original-value``；
- 模板替换完成后若仍有占位符残留，抛 :class:`ArtifactError`。
"""

from __future__ import annotations

import html
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path

from memoloupe.analysis.observations import Observation, Source, ValueState
from memoloupe.analysis.resolvers import DEFAULT_RESOLVERS, build_observations_with_review
from memoloupe.analysis.vocabulary import FieldRule, Vocabulary, load_vocabulary
from memoloupe.core.atomic_io import read_json, write_text_atomic
from memoloupe.core.errors import ArtifactError, ContractError
from memoloupe.validate.html_contract import DOCUMENT_STATUSES

SHOT_RENDER_VERSION = "render.v1"
CONTRACT_VERSION = "1.0"
DOCUMENT_TYPE = "shotAnalysis"

_TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "templates" / "shot-analysis.html"

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
)

#: 非 value 状态的固定可见文案（docs/04 §3.3：absent 与 absent-claimed 必须不同）。
_STATE_TEXT: dict[ValueState, str] = {
    ValueState.ABSENT: "无（确定性检测）",
    ValueState.ABSENT_CLAIMED: "模型声称无",
    ValueState.UNKNOWN: "未知",
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


def _state_visible_text(obs: Observation) -> str:
    if obs.state == ValueState.VALUE:
        value = obs.value
        if isinstance(value, list):
            return html.escape("、".join(str(item) for item in value) if value else "无标记")
        return html.escape(str(value))
    if obs.state == ValueState.UNMAPPED:
        return "未映射：" + html.escape(str(obs.original_value))
    return _STATE_TEXT[obs.state]


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
        extra = ("", f"{obs.original_value}（待映射）")
    else:
        extra = ("", _STATE_TEXT[obs.state])
    parts: list[str] = []
    for opt in options:
        selected = " selected" if current == opt else ""
        parts.append(f'<option value="{html.escape(opt)}"{selected}>{html.escape(opt)}</option>')
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
    # confidence=unknown 也必须可见（docs/04 §3.3）。
    parts.append(f'<span class="cell-confidence">置信度 {html.escape(obs.confidence.value)}</span>')
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
    frame_ref: str | None,
    clip_src: str | None,
    review_reasons: list[str],
) -> str:
    shot_id = str(shot.get("shotID", ""))
    start_ms = shot.get("finalStartMs")
    end_ms = shot.get("finalEndMs")
    duration_ms = shot.get("durationMs")
    # needsReview：合并 shots.json 标记与 resolver 报告的 review 理由（D-005）。
    needs_review = bool(shot.get("needsReview")) or bool(review_reasons)
    esc_id = html.escape(shot_id)

    title_attr = ""
    if review_reasons:
        title_attr = f' title="{html.escape("；".join(review_reasons))}"'
    elif needs_review:
        title_attr = ' title="shots.json 标记 needsReview"'

    lines = [
        f'<th scope="col" data-shot-id="{esc_id}" data-start-ms="{start_ms}" '
        f'data-end-ms="{end_ms}" data-needs-review="{"true" if needs_review else "false"}"'
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
    if frame_ref is not None:
        lines.append(
            f'<img class="shot-frame" src="{html.escape(frame_ref)}" '
            f'alt="镜头 {esc_id} 代表帧" loading="lazy">'
        )
    if needs_review:
        lines.append('<span class="needs-review-badge">⚠ 需人工复核</span>')
    if clip_src is not None:
        lines.append(
            f'<button type="button" class="play-btn" data-clip-src="{html.escape(clip_src)}" '
            f'data-start-ms="{start_ms}" data-end-ms="{end_ms}" '
            f'aria-label="播放镜头 {esc_id}">▶ 播放</button>'
        )
    else:
        lines.append(
            f'<button type="button" class="play-btn" disabled '
            f'aria-label="镜头 {esc_id} 无 clip，无法播放">▶ 无 clip</button>'
        )
    if isinstance(start_ms, int) and isinstance(end_ms, int):
        # 边界修正表单：提交进 pendingChanges（kind="boundary"），最终校验在应用端。
        lines.append(
            f'<form class="boundary-form" data-shot-id="{esc_id}" '
            f'aria-label="{esc_id} 边界修正">'
            f'<label>finalStartMs <input type="number" name="finalStartMs" '
            f'value="{start_ms}" min="0" aria-label="{esc_id} finalStartMs"></label>'
            f'<label>finalEndMs <input type="number" name="finalEndMs" '
            f'value="{end_ms}" min="0" aria-label="{esc_id} finalEndMs"></label>'
            f'<button type="submit">提交边界修正</button></form>'
        )
    lines.append("</div></th>")
    return "".join(lines)


def _table_html(
    shots: list[dict],
    observations_by_shot: dict[str, list[Observation]],
    review_reasons_by_shot: dict[str, list[str]],
    frame_refs: dict[str, str],
    out_dir: Path,
    vocabulary: Vocabulary,
) -> str:
    head_cells = ['<th scope="col">字段 \\ 镜头</th>']
    for shot in shots:
        shot_id = str(shot.get("shotID", ""))
        clip_path = out_dir / "clips" / f"{shot_id}.mp4"
        clip_src = f"clips/{shot_id}.mp4" if clip_path.is_file() else None
        head_cells.append(
            _column_header_html(
                shot, frame_refs.get(shot_id), clip_src,
                review_reasons_by_shot.get(shot_id, []),
            )
        )
    rows = [
        "<table>",
        f"<thead><tr>{''.join(head_cells)}</tr></thead>",
        "<tbody>",
    ]
    if shots:
        field_count = len(observations_by_shot[str(shots[0].get("shotID", ""))])
        for row_index in range(field_count):
            first_obs = observations_by_shot[str(shots[0].get("shotID", ""))][row_index]
            cells = [
                f'<th scope="row" data-field="{html.escape(first_obs.field)}">'
                f"{html.escape(first_obs.field)}</th>"
            ]
            for shot in shots:
                obs = observations_by_shot[str(shot.get("shotID", ""))][row_index]
                cells.append(_cell_html(obs, vocabulary.fields.get(obs.field)))
            rows.append(f"<tr>{''.join(cells)}</tr>")
    rows.append("</tbody></table>")
    return "\n".join(rows)


def _metadata_html(status: str, shot_count: int, revision: str) -> str:
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entries = [
        ("文档状态", status),
        ("镜头数", str(shot_count)),
        ("源 revision", revision),
        ("生成时间", generated),
        ("契约版本", CONTRACT_VERSION),
        ("渲染版本", SHOT_RENDER_VERSION),
    ]
    items = "".join(
        f"<div><dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd></div>"
        for label, value in entries
    )
    return (
        '<header class="metadata" id="metadata">'
        "<h1>Shot Analysis 校对视图</h1>"
        f'<dl class="metadata-grid">{items}</dl>'
        "</header>"
    )


def _validation_html(validation_summary: str | None, warnings: list[str]) -> str:
    """校验摘要区（只读）：外部校验结果 + corrections overlay 警告。"""
    parts = [
        '<section id="validation-summary" aria-label="校验摘要">',
        "<h2>校验摘要</h2>",
    ]
    if validation_summary is None:
        parts.append('<p class="validation-empty">未提供校验摘要</p>')
    else:
        parts.append(f'<pre class="validation-report">{html.escape(validation_summary)}</pre>')
    if warnings:
        parts.append('<ul class="correction-warnings">')
        for warning in warnings:
            parts.append(f"<li>{html.escape(warning)}</li>")
        parts.append("</ul>")
    parts.append("</section>")
    return "".join(parts)


def _js_string(value: str) -> str:
    """转成安全的 JS 字符串字面量（json.dumps + 转义 "</"，防 </script> 逃逸）。"""
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


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

    document = _TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "__DOCUMENT_STATUS__": document_status,
        "__CONTRACT_VERSION__": CONTRACT_VERSION,
        "__SOURCE_REVISION__": html.escape(revision),
        "__SOURCE_REVISION_JS__": _js_string(revision),
        "__SERVER_MODE__": "true" if server_mode else "false",
        "__REVIEW_REASONS_JSON__": json.dumps(
            review_reasons_by_shot, ensure_ascii=False
        ).replace("</", "<\\/"),
        "<!--METADATA-->": _metadata_html(document_status, len(shots), revision),
        "<!--VALIDATION_SUMMARY-->": _validation_html(validation_summary, warnings),
        "<!--SHOT_TABLE-->": _table_html(
            shots, observations_by_shot, review_reasons_by_shot,
            _frame_refs(raws, out_dir), out_dir, vocabulary,
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
