"""analysis.completion — completion 规则评估与文档显式确认（docs/04 §2/§9）。

``rules/completion.json`` 声明每类文档的完成条件；``confirmed`` 必须是用户
显式动作，且确认前必须通过 completion 评估、跨文件严格校验与 HTML 严格
校验三道闸门。任何一道不满足都拒绝写入并返回人类可读原因。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from memoloupe.analysis.observations import Observation, ValueState
from memoloupe.analysis.resolvers import DEFAULT_RESOLVERS, build_observations
from memoloupe.core.atomic_io import read_json
from memoloupe.core.errors import ContractError, EvidenceRefError
from memoloupe.core.evidence_refs import parse_evidence_ref
from memoloupe.core.packaged import packaged_path
from memoloupe.validate.cross_artifact import validate_output_dir
from memoloupe.validate.html_contract import validate_html

COMPLETION_VERSION = "completion.v1"

#: 仓库根/rules/completion.json（本文件位于 src/memoloupe/analysis/completion.py）。
_RULES_PATH = packaged_path("rules", "completion.json")

#: 渲染器读取的 raw 逻辑名（与 render.shot_html.RAW_FILES 保持一致）。
_RAW_FILES: tuple[str, ...] = (
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

#: documentType → 对应 HTML 文件名。
_HTML_FILES: dict[str, str] = {
    "shotAnalysis": "shot-analysis.html",
    "storyAnalysis": "shot-analysis.html",  # 故事结果合并呈现在 shot 工作台
}

#: requiredFields 视为已解决的取值状态。
_RESOLVED_STATES = frozenset({ValueState.VALUE, ValueState.ABSENT})


@dataclass(frozen=True)
class CompletionReport:
    """一次 completion 评估结果；``unmet`` 含 shotID/field 定位。"""

    document_type: str
    satisfied: bool
    unmet: tuple[str, ...]


def _default_evidence_validator(ref: str) -> None:
    parse_evidence_ref(ref)


def evaluate_completion(
    document_type: str,
    observations: list[Observation],
    rules: dict,
    evidence_validator: Callable[[str], None] | None = None,
) -> CompletionReport:
    """按 rules（rules/completion.json 内容）评估文档是否满足完成条件。

    规则（shotAnalysis 族）：

    - ``requiredFields``：每镜头该字段必须存在且 state ∈ {value, absent}；
      state=unknown 时按 ``allowUnknown`` 决定（true 放过，false 记未满足）；
    - ``requireVerifiedStates``：处于这些状态（如 unmapped/absent-claimed）
      的观察必须 ``verified=true``；
    - ``allowUnknown=false``：任何 unknown 观察都记未满足；
    - ``requireValidEvidenceRefs=true``：非 unknown 观察的全部
      evidence_refs 必须过 ``parse_evidence_ref``（或注入的校验器）。

    ``document_type`` 在 rules 中无对应规则时视为满足（空 unmet）。
    """
    document_rules = rules.get("documents", {}).get(document_type)
    if not isinstance(document_rules, dict):
        return CompletionReport(document_type, True, ())

    validate_ref = evidence_validator or _default_evidence_validator
    required_fields = tuple(document_rules.get("requiredFields", ()))
    verified_states = {
        ValueState(s) for s in document_rules.get("requireVerifiedStates", ())
    }
    allow_unknown = bool(document_rules.get("allowUnknown", False))
    check_refs = bool(document_rules.get("requireValidEvidenceRefs", False))

    unmet: list[str] = []
    by_key: dict[tuple[str, str], Observation] = {}
    shot_ids: list[str] = []
    for obs in observations:
        by_key[(obs.shot_id, obs.field)] = obs
        if obs.shot_id not in shot_ids:
            shot_ids.append(obs.shot_id)

    for shot_id in shot_ids:
        for field in required_fields:
            obs = by_key.get((shot_id, field))
            if obs is None:
                unmet.append(f"{shot_id}.{field}: 缺 requiredField 观察")
                continue
            if obs.state in _RESOLVED_STATES:
                continue
            if obs.state == ValueState.UNKNOWN and allow_unknown:
                continue
            unmet.append(
                f"{shot_id}.{field}: requiredField state={obs.state.value} "
                "未解决（要求 value/absent）"
            )

    for obs in observations:
        if obs.state in verified_states and not obs.verified:
            unmet.append(
                f"{obs.shot_id}.{obs.field}: state={obs.state.value} 必须 verified=true"
            )
        if obs.state == ValueState.UNKNOWN and not allow_unknown:
            unmet.append(f"{obs.shot_id}.{obs.field}: unknown 不被 allowUnknown 允许")
        if check_refs and obs.state != ValueState.UNKNOWN:
            for ref in obs.evidence_refs:
                try:
                    validate_ref(ref)
                except (EvidenceRefError, ValueError, TypeError) as exc:
                    unmet.append(
                        f"{obs.shot_id}.{obs.field}: evidenceRef 非法：{ref!r}（{exc}）"
                    )

    return CompletionReport(document_type, not unmet, tuple(unmet))


def _load_raws(out_dir: Path) -> dict[str, dict | None]:
    raws: dict[str, dict | None] = {}
    for name in _RAW_FILES:
        try:
            raws[name] = read_json(out_dir / "raw" / f"{name}.json")
        except ContractError:
            raws[name] = None
    return raws


def _build_all_observations(out_dir: Path) -> tuple[list[Observation], str | None]:
    """从 out_dir 的 raw 构建全部镜头观察；失败返回 (空, 原因)。"""
    raws = _load_raws(out_dir)
    shots_doc = raws.get("shots")
    if not shots_doc or not isinstance(shots_doc.get("shots"), list):
        return [], "raw/shots.json 缺失或不可读，无法评估 completion"
    shot_ids = [
        s["shotID"]
        for s in shots_doc["shots"]
        if isinstance(s, dict) and isinstance(s.get("shotID"), str)
    ]
    observations: list[Observation] = []
    for shot_id in shot_ids:
        observations.extend(build_observations(shot_id, raws, DEFAULT_RESOLVERS))
    return observations, None


def _current_revision(out_dir: Path) -> str:
    try:
        media = read_json(Path(out_dir) / "raw" / "media.json")
    except ContractError:
        return ""
    value = media.get("source", {}).get("revisionID")
    return value if isinstance(value, str) else ""


def confirm_document(
    out_dir: Path, document_type: str, *, actor: str = "human"
) -> tuple[bool, list[str]]:
    """显式确认文档：三道闸门全部通过才写入 confirmedAt/confirmedBy。

    闸门：completion 评估通过 + ``validate_output_dir(strict=True)`` 无 error
    + ``validate_html(对应 html, root=out_dir, strict=True)`` 无 error。
    文档状态已是 ``outdated`` 时直接拒绝。返回 ``(成功否, 失败原因列表)``。
    """
    from memoloupe.render.corrections import (
        apply_corrections,
        confirm_corrections,
        document_status,
        load_corrections,
    )

    out_dir = Path(out_dir)
    reasons: list[str] = []
    revision = _current_revision(out_dir)
    corrections = load_corrections(out_dir, document_type)

    if document_status(corrections, revision) == "outdated":
        return False, [
            f"文档已 outdated（corrections 基于 revision "
            f"{corrections.source_revision_id}，当前 {revision}），禁止确认"
        ]

    observations, error = _build_all_observations(out_dir)
    if error is not None:
        reasons.append(error)
    else:
        observations, _warnings = apply_corrections(observations, corrections, revision)
        report = evaluate_completion(
            document_type, observations, read_json(_RULES_PATH)
        )
        reasons.extend(report.unmet)

    for issue in validate_output_dir(out_dir, strict=True):
        if issue.severity == "error":
            reasons.append(
                f"跨文件校验 {issue.artifact}{issue.json_path}: {issue.message}"
            )

    html_name = _HTML_FILES.get(document_type)
    if html_name is None:
        reasons.append(f"未知 documentType：{document_type!r}，无对应 HTML")
    else:
        for issue in validate_html(out_dir / html_name, root=out_dir, strict=True):
            if issue.severity == "error":
                reasons.append(f"HTML 校验 {html_name} {issue.json_path}: {issue.message}")

    if reasons:
        return False, reasons

    confirm_corrections(out_dir, document_type, actor=actor)
    return True, []
