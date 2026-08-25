"""render.corrections — 人工修正 overlay 的读写、状态推导与应用（docs/02 §6、docs/04 §5）。

渲染顺序固定为 raw → resolver → corrections overlay → HTML：

- 修正存于 ``corrections/<documentType>.json``，永不覆盖 raw 文件；
- 历史 ``changes`` 只追加、永不删改；同 ``entityID+field`` 的多次修正全部
  保留，应用时取最后一条；
- 每条修正带 ``sourceRevisionID``；源 revision 变化后文档状态为
  ``outdated``，旧修正不自动套用（:func:`apply_corrections` 原样返回并记
  warning）；
- ``confirmed`` 必须是用户显式动作，不得由 verified 全选推导；
- ``kind="boundary"`` 的修正不在字段 overlay 中处理，由渲染层经
  :func:`boundary_changes` 单独消费（docs/00 §4.5 边界双轨）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import jsonschema

from memoloupe.analysis.observations import (
    DETERMINISTIC_SOURCES,
    Observation,
    Source,
    ValueState,
)
from memoloupe.core.atomic_io import read_json, write_json_atomic
from memoloupe.core.errors import ContractError

CORRECTIONS_VERSION = "corrections.v1"

#: 仓库根/schemas（本文件位于 src/memoloupe/render/corrections.py）。
_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "corrections.json"

#: 文档状态取值（docs/04 §2）；outdated 优先于一切。
STATUS_DRAFT = "draft"
STATUS_UNDER_REVIEW = "underReview"
STATUS_CONFIRMED = "confirmed"
STATUS_OUTDATED = "outdated"


@dataclass(frozen=True)
class Corrections:
    """一份文档的人工修正 overlay。

    ``path`` 为 None 表示尚无落盘文件（空骨架）。
    """

    document_type: str
    source_revision_id: str
    changes: tuple[dict, ...]
    confirmed_at: str | None
    confirmed_by: str | None
    path: Path | None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def corrections_path(out_dir: Path, document_type: str) -> Path:
    """corrections 文件路径：``corrections/<documentType>.json``。"""
    return Path(out_dir) / "corrections" / f"{document_type}.json"


def _validate_document(data: dict, artifact: str) -> None:
    """按 schemas/corrections.json 校验，失败抛 :class:`ContractError`。"""
    schema = read_json(_SCHEMA_PATH)
    validator = jsonschema.Draft202012Validator(schema)
    error = jsonschema.exceptions.best_match(validator.iter_errors(data))
    if error is None:
        return
    path = "$"
    for part in error.absolute_path:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    raise ContractError(
        artifact=artifact,
        json_path=path,
        expected=error.message,
        actual=repr(error.instance)[:120],
    )


def _from_document(data: dict, path: Path | None) -> Corrections:
    return Corrections(
        document_type=data["documentType"],
        source_revision_id=data["sourceRevisionID"],
        changes=tuple(data["changes"]),
        confirmed_at=data.get("confirmedAt"),
        confirmed_by=data.get("confirmedBy"),
        path=path,
    )


def load_corrections(out_dir: Path, document_type: str) -> Corrections:
    """读取 corrections overlay；文件不存在时返回空骨架。

    文件存在但不符合 schemas/corrections.json 时抛 :class:`ContractError`。
    空骨架的 ``source_revision_id`` 为空串，由调用方按需填充。
    """
    path = corrections_path(out_dir, document_type)
    if not path.exists():
        return Corrections(
            document_type=document_type,
            source_revision_id="",
            changes=(),
            confirmed_at=None,
            confirmed_by=None,
            path=None,
        )
    data = read_json(path)
    _validate_document(data, artifact=path.name)
    return _from_document(data, path)


def _to_document(corrections: Corrections) -> dict:
    doc: dict[str, object] = {
        "correctionVersion": 1,
        "documentType": corrections.document_type,
        "sourceRevisionID": corrections.source_revision_id,
        "changes": list(corrections.changes),
    }
    if corrections.confirmed_at is not None or corrections.confirmed_by is not None:
        doc["confirmedAt"] = corrections.confirmed_at
        doc["confirmedBy"] = corrections.confirmed_by
    return doc


def _write_corrections(out_dir: Path, corrections: Corrections) -> Corrections:
    """schema 校验后原子写入，返回带 path 的 Corrections。"""
    path = corrections_path(out_dir, corrections.document_type)
    doc = _to_document(corrections)
    _validate_document(doc, artifact=path.name)
    write_json_atomic(path, doc)
    return replace(corrections, path=path)


def append_changes(
    out_dir: Path,
    document_type: str,
    source_revision_id: str,
    new_changes: list[dict],
    *,
    actor: str = "human",
) -> Corrections:
    """追加修正：历史 changes 永不删改，每项补 ``changedAt``/``actor``。

    写前对合并后的完整文档做 schema 校验（失败抛 :class:`ContractError`，
    不落盘），写入走临时文件加原子替换。
    """
    existing = load_corrections(out_dir, document_type)
    stamped = []
    for change in new_changes:
        entry = dict(change)
        entry.setdefault("changedAt", _utc_now_iso())
        entry.setdefault("actor", actor)
        stamped.append(entry)
    merged = replace(
        existing,
        source_revision_id=source_revision_id,
        changes=existing.changes + tuple(stamped),
    )
    return _write_corrections(out_dir, merged)


def confirm_corrections(
    out_dir: Path, document_type: str, *, actor: str = "human"
) -> Corrections:
    """写入显式确认字段（confirmedAt/confirmedBy），保留全部历史 changes。"""
    existing = load_corrections(out_dir, document_type)
    confirmed = replace(
        existing,
        confirmed_at=_utc_now_iso(),
        confirmed_by=actor,
    )
    return _write_corrections(out_dir, confirmed)


def document_status(corrections: Corrections, current_revision: str) -> str:
    """推导文档状态（docs/04 §2）；``outdated`` 优先于一切。

    ``confirmed`` 只来自显式的 ``confirmed_at``，不得由 verified 全选推导。
    """
    if corrections.source_revision_id and corrections.source_revision_id != current_revision:
        return STATUS_OUTDATED
    if corrections.confirmed_at is not None:
        return STATUS_CONFIRMED
    if corrections.changes:
        return STATUS_UNDER_REVIEW
    return STATUS_DRAFT


def _apply_field_change(obs: Observation, change: dict) -> Observation:
    """按 change 构造修正后的 Observation。

    与 :func:`memoloupe.analysis.observations.apply_human_correction` 同一
    语义：``source=human``、``original_value`` 记录修正前的 value；差别仅在于
    ``verified`` 按 change 取值（支持"取消核实"）。
    """
    new_state = ValueState(change["state"])
    if new_state == ValueState.ABSENT and Source(obs.source) not in DETERMINISTIC_SOURCES:
        raise ValueError(
            f"不能仅经人工核实把 source={obs.source} 的观察改为 absent；"
            "absent 只能来自授权确定性检测器"
        )
    return replace(
        obs,
        value=change.get("newValue"),
        state=new_state,
        source=Source.HUMAN,
        verified=bool(change.get("verified", True)),
        original_value=obs.value,
    )


def apply_corrections(
    observations: list[Observation],
    corrections: Corrections,
    current_revision: str,
) -> tuple[list[Observation], list[str]]:
    """应用字段修正 overlay，返回 ``(修正后的 observations, warnings)``。

    - ``source_revision_id`` 非空且与 ``current_revision`` 不一致时不应用任何
      修正，原样返回并记 warning（旧修正不自动套用新 revision）；
    - 命中的 ``(entityID=shot_id, field)`` 取最后一条 change；
    - 不匹配任何 observation 的 change 记 warning，不报错；
    - 违反 absent 限制（非确定性来源改 absent）的 change 记 warning 并跳过；
    - ``kind="boundary"`` 的 change 不在此处理。
    """
    warnings: list[str] = []
    if (
        corrections.source_revision_id
        and corrections.source_revision_id != current_revision
    ):
        warnings.append(
            f"corrections 基于 revision {corrections.source_revision_id}，"
            f"与当前 revision {current_revision} 不一致，不应用任何修正"
        )
        return list(observations), warnings

    # 同 key 多次修正取最后一条（历史全部保留在文件中）。
    latest: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for change in corrections.changes:
        if change.get("kind", "field") == "boundary":
            continue
        key = (str(change.get("entityID", "")), str(change.get("field", "")))
        if key not in latest:
            order.append(key)
        latest[key] = change

    by_key: dict[tuple[str, str], list[int]] = {}
    for index, obs in enumerate(observations):
        by_key.setdefault((obs.shot_id, obs.field), []).append(index)

    result = list(observations)
    for key in order:
        indices = by_key.get(key)
        if not indices:
            warnings.append(
                f"修正 {key[0]}.{key[1]} 不匹配任何 observation，已跳过"
            )
            continue
        change = latest[key]
        for index in indices:
            try:
                result[index] = _apply_field_change(result[index], change)
            except (ValueError, KeyError) as exc:
                warnings.append(f"修正 {key[0]}.{key[1]} 无法应用：{exc}")
    return result, warnings


def boundary_changes(corrections: Corrections) -> list[dict]:
    """提取 ``kind="boundary"`` 的修正，由渲染层单独消费（边界双轨）。"""
    return [
        change
        for change in corrections.changes
        if change.get("kind", "field") == "boundary"
    ]
