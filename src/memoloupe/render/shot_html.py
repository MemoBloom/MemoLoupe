"""render.shot_html — shot-analysis.html 渲染器（docs/04 §2/§3/§6）。

流程固定为：读 output-dir 的 raw/*.json（缺失容忍为 None）→ 经
:mod:`memoloupe.analysis.resolvers` 生成 Observation → 映射到
``templates/shot-analysis.html`` 骨架（占位符字符串替换）→ 原子写入。

- 所有模型/检测原文经 ``html.escape`` 后才进入 HTML；
- CSS/JS 由模板固定内联，绝不动态拼接用户内容（CSP 例外因此安全）；
- 媒体一律相对路径；缺 clips/SHxxxx.mp4 时播放按钮禁用；
- 模板替换完成后若仍有占位符残留，抛 :class:`ArtifactError`。
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path

from memoloupe.analysis.observations import Observation, ValueState
from memoloupe.analysis.resolvers import DEFAULT_RESOLVERS, build_observations
from memoloupe.core.atomic_io import read_json, write_text_atomic
from memoloupe.core.errors import ArtifactError, ContractError
from memoloupe.validate.html_contract import DOCUMENT_STATUSES

SHOT_RENDER_VERSION = "render.v1"
CONTRACT_VERSION = "1.0"

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


def _cell_html(obs: Observation) -> str:
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
    state_class = f"state-{obs.state.value}"
    parts = [f'<span class="cell-text {state_class}">{_state_visible_text(obs)}</span>']
    # confidence=unknown 也必须可见（docs/04 §3.3）。
    parts.append(f'<span class="cell-confidence">置信度 {html.escape(obs.confidence.value)}</span>')
    if obs.verified:
        parts.append('<span class="cell-verified">已核实</span>')
    return f"<td {attrs}>{''.join(parts)}</td>"


def _column_header_html(
    shot: dict, frame_ref: str | None, clip_src: str | None
) -> str:
    shot_id = str(shot.get("shotID", ""))
    start_ms = shot.get("finalStartMs")
    end_ms = shot.get("finalEndMs")
    duration_ms = shot.get("durationMs")
    needs_review = bool(shot.get("needsReview"))
    esc_id = html.escape(shot_id)

    lines = [
        f'<th scope="col" data-shot-id="{esc_id}" data-start-ms="{start_ms}" '
        f'data-end-ms="{end_ms}" data-needs-review="{"true" if needs_review else "false"}">',
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
    lines.append("</div></th>")
    return "".join(lines)


def _table_html(
    shots: list[dict],
    observations_by_shot: dict[str, list[Observation]],
    frame_refs: dict[str, str],
    out_dir: Path,
) -> str:
    head_cells = ['<th scope="col">字段 \\ 镜头</th>']
    for shot in shots:
        shot_id = str(shot.get("shotID", ""))
        clip_path = out_dir / "clips" / f"{shot_id}.mp4"
        clip_src = f"clips/{shot_id}.mp4" if clip_path.is_file() else None
        head_cells.append(
            _column_header_html(shot, frame_refs.get(shot_id), clip_src)
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
                cells.append(_cell_html(obs))
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


def render_shot_html(out_dir: Path, *, status: str = "draft") -> Path:
    """渲染 out_dir 的 shot-analysis.html 并原子写入，返回写入路径。

    shots.json 缺失/不可读时抛 :class:`ArtifactError`（无镜头列可渲染）；
    其余 raw 文件缺失时对应字段落 unknown。
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

    observations_by_shot = {
        str(shot["shotID"]): build_observations(str(shot["shotID"]), raws, DEFAULT_RESOLVERS)
        for shot in shots
    }
    revision = "unknown"
    media = raws.get("media")
    if media:
        value = media.get("source", {}).get("revisionID")
        if isinstance(value, str) and value:
            revision = value

    document = _TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "__DOCUMENT_STATUS__": status,
        "__CONTRACT_VERSION__": CONTRACT_VERSION,
        "__SOURCE_REVISION__": html.escape(revision),
        "<!--METADATA-->": _metadata_html(status, len(shots), revision),
        "<!--SHOT_TABLE-->": _table_html(
            shots, observations_by_shot, _frame_refs(raws, out_dir), out_dir
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
