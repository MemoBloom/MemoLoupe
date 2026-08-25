"""render.corrections 单元测试：人工修正数据层（docs/02 §6、docs/04 §5）。

覆盖：corrections schema 校验、load 缺失骨架、append 追加语义与原子性、
document_status 四态迁移与 outdated 优先、apply_corrections 命中/取最新/
原值保留/revision 不匹配/孤立 change、boundary_changes 提取。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from memoloupe.analysis.observations import (
    Source,
    ValueState,
    model_value_observation,
)
from memoloupe.core.atomic_io import read_json
from memoloupe.core.errors import ContractError
from memoloupe.render.corrections import (
    CORRECTIONS_VERSION,
    Corrections,
    append_changes,
    apply_corrections,
    boundary_changes,
    document_status,
    load_corrections,
)

SCHEMA_PATH = (
    Path(__file__).parent.parent.parent / "schemas" / "corrections.json"
)
REV = "a1b2c3d4e5f6"


def _valid_doc() -> dict:
    """docs/02 §6 示例补齐规范字段后的合法文档。"""
    return {
        "correctionVersion": 1,
        "documentType": "shotAnalysis",
        "sourceRevisionID": REV,
        "changes": [
            {
                "entityID": "SH0001",
                "field": "visual.framing",
                "oldValue": "全景",
                "newValue": "中景",
                "state": "value",
                "verified": True,
                "changedAt": "2026-08-21T08:00:00Z",
                "actor": "human",
            }
        ],
    }


def _change(field: str = "visual.framing", entity: str = "SH0001", **kw) -> dict:
    change = {
        "entityID": entity,
        "field": field,
        "oldValue": "全景",
        "newValue": "中景",
        "state": "value",
        "verified": True,
        "changedAt": "2026-08-21T08:00:00Z",
        "actor": "human",
    }
    change.update(kw)
    return change


def _obs(
    field: str = "visual.framing",
    shot_id: str = "SH0001",
    value: object = "全景",
    source: Source = Source.UNIFIED_MODEL,
) -> object:
    return model_value_observation(
        field,
        shot_id,
        value,
        evidence_refs=("raw/unified-media.json#batches[0].response.shots[0].visual.framing",),
        source=source,
    )


class TestCorrectionsSchema:
    def _validator(self) -> Draft202012Validator:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        return Draft202012Validator(schema)

    def test_schema_is_draft_2020_12_with_stable_id(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"] == "https://memoloupe.local/schemas/corrections.json"

    def test_doc_example_variant_valid(self):
        assert self._validator().is_valid(_valid_doc())

    def test_optional_fields_valid(self):
        doc = _valid_doc()
        doc["changes"][0]["note"] = "人工复核"
        doc["changes"][0]["kind"] = "boundary"
        doc["confirmedAt"] = "2026-08-21T09:00:00Z"
        doc["confirmedBy"] = "human"
        assert self._validator().is_valid(doc)
        doc["confirmedAt"] = None
        doc["confirmedBy"] = None
        assert self._validator().is_valid(doc)

    def test_null_old_and_new_value_valid(self):
        doc = _valid_doc()
        doc["changes"][0]["oldValue"] = None
        doc["changes"][0]["newValue"] = None
        assert self._validator().is_valid(doc)

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.update({"correctionVersion": 2}),
            lambda d: d.update({"documentType": "otherDoc"}),
            lambda d: d["changes"][0].update({"state": "half-known"}),
            lambda d: d["changes"][0].update({"verified": "yes"}),
            lambda d: d["changes"][0].pop("actor"),
            lambda d: d["changes"][0].pop("changedAt"),
            lambda d: d["changes"][0].pop("entityID"),
            lambda d: d.pop("sourceRevisionID"),
            lambda d: d.pop("changes"),
            lambda d: d.update({"confirmedAt": 123}),
        ],
    )
    def test_invalid_variants_rejected(self, mutate):
        doc = _valid_doc()
        mutate(doc)
        assert not self._validator().is_valid(doc)


class TestLoadCorrections:
    def test_missing_file_returns_empty_skeleton(self, tmp_path):
        corrections = load_corrections(tmp_path, "shotAnalysis")
        assert isinstance(corrections, Corrections)
        assert corrections.document_type == "shotAnalysis"
        assert corrections.source_revision_id == ""
        assert corrections.changes == ()
        assert corrections.confirmed_at is None
        assert corrections.confirmed_by is None
        assert corrections.path is None

    def test_existing_file_loaded(self, tmp_path):
        path = tmp_path / "corrections" / "shotAnalysis.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(_valid_doc()), encoding="utf-8")
        corrections = load_corrections(tmp_path, "shotAnalysis")
        assert corrections.source_revision_id == REV
        assert len(corrections.changes) == 1
        assert corrections.changes[0]["entityID"] == "SH0001"
        assert corrections.path == path

    def test_invalid_file_raises_contract_error(self, tmp_path):
        path = tmp_path / "corrections" / "shotAnalysis.json"
        path.parent.mkdir(parents=True)
        doc = _valid_doc()
        doc["correctionVersion"] = 2
        path.write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(ContractError):
            load_corrections(tmp_path, "shotAnalysis")

    def test_version_constant(self):
        assert CORRECTIONS_VERSION == "corrections.v1"


class TestAppendChanges:
    def test_append_creates_file_and_fills_metadata(self, tmp_path):
        corrections = append_changes(
            tmp_path, "shotAnalysis", REV, [_change()], actor="human"
        )
        assert corrections.path == tmp_path / "corrections" / "shotAnalysis.json"
        assert corrections.source_revision_id == REV
        assert len(corrections.changes) == 1
        change = corrections.changes[0]
        assert change["actor"] == "human"
        assert change["changedAt"].endswith("Z")
        # 文件落盘且通过 schema 校验。
        on_disk = read_json(corrections.path)
        assert on_disk["correctionVersion"] == 1
        assert len(on_disk["changes"]) == 1

    def test_append_is_additive_and_keeps_history(self, tmp_path):
        append_changes(tmp_path, "shotAnalysis", REV, [_change(newValue="中景")])
        corrections = append_changes(
            tmp_path, "shotAnalysis", REV, [_change(newValue="近景")]
        )
        # 同 entityID+field 的多次修正全部保留。
        assert len(corrections.changes) == 2
        assert [c["newValue"] for c in corrections.changes] == ["中景", "近景"]

    def test_append_rejects_schema_violation_without_touching_file(self, tmp_path):
        append_changes(tmp_path, "shotAnalysis", REV, [_change()])
        before = read_json(tmp_path / "corrections" / "shotAnalysis.json")
        bad = _change()
        bad["state"] = "half-known"
        with pytest.raises(ContractError):
            append_changes(tmp_path, "shotAnalysis", REV, [bad])
        assert read_json(tmp_path / "corrections" / "shotAnalysis.json") == before

    def test_append_keeps_confirmed_fields(self, tmp_path):
        append_changes(tmp_path, "shotAnalysis", REV, [_change()])
        path = tmp_path / "corrections" / "shotAnalysis.json"
        doc = read_json(path)
        doc["confirmedAt"] = "2026-08-21T09:00:00Z"
        doc["confirmedBy"] = "human"
        path.write_text(json.dumps(doc), encoding="utf-8")
        corrections = append_changes(tmp_path, "shotAnalysis", REV, [_change()])
        assert corrections.confirmed_at == "2026-08-21T09:00:00Z"
        assert corrections.confirmed_by == "human"


class TestDocumentStatus:
    def _corrections(self, revision=REV, changes=(), confirmed_at=None) -> Corrections:
        return Corrections(
            document_type="shotAnalysis",
            source_revision_id=revision,
            changes=changes,
            confirmed_at=confirmed_at,
            confirmed_by="human" if confirmed_at else None,
            path=None,
        )

    def test_draft_when_empty(self):
        assert document_status(self._corrections(), REV) == "draft"

    def test_under_review_with_changes(self):
        c = self._corrections(changes=(_change(),))
        assert document_status(c, REV) == "underReview"

    def test_confirmed_explicit_only(self):
        c = self._corrections(changes=(_change(),), confirmed_at="2026-08-21T09:00:00Z")
        assert document_status(c, REV) == "confirmed"
        # confirmed 不得由 verified 全选推导：无 confirmedAt 时即使有修正也只是 underReview。
        assert document_status(self._corrections(changes=(_change(),)), REV) == "underReview"

    def test_outdated_beats_everything(self):
        c = self._corrections(
            revision="old-rev", changes=(_change(),), confirmed_at="2026-08-21T09:00:00Z"
        )
        assert document_status(c, REV) == "outdated"
        # 空 source_revision_id 不判 outdated。
        assert document_status(self._corrections(revision=""), REV) == "draft"


class TestApplyCorrections:
    def test_hit_applies_human_semantics(self):
        obs = _obs()
        corrections = Corrections(
            document_type="shotAnalysis",
            source_revision_id=REV,
            changes=(_change(),),
            confirmed_at=None,
            confirmed_by=None,
            path=None,
        )
        result, warnings = apply_corrections([obs], corrections, REV)
        assert warnings == []
        (new_obs,) = result
        assert new_obs.value == "中景"
        assert new_obs.state == ValueState.VALUE
        assert new_obs.source == Source.HUMAN
        assert new_obs.verified is True
        assert new_obs.original_value == "全景"

    def test_last_change_wins(self):
        obs = _obs()
        changes = (_change(newValue="中景"), _change(newValue="近景"))
        corrections = Corrections("shotAnalysis", REV, changes, None, None, None)
        result, warnings = apply_corrections([obs], corrections, REV)
        assert warnings == []
        assert result[0].value == "近景"

    def test_verified_false_from_change_respected(self):
        obs = _obs()
        corrections = Corrections(
            "shotAnalysis", REV, (_change(verified=False),), None, None, None
        )
        result, _ = apply_corrections([obs], corrections, REV)
        assert result[0].verified is False
        assert result[0].source == Source.HUMAN

    def test_revision_mismatch_applies_nothing(self):
        obs = _obs()
        corrections = Corrections("shotAnalysis", "old-rev", (_change(),), None, None, None)
        result, warnings = apply_corrections([obs], corrections, REV)
        assert result == [obs]
        assert len(warnings) == 1
        assert "revision" in warnings[0].lower() or "old-rev" in warnings[0]

    def test_orphan_change_warns_but_does_not_fail(self):
        obs = _obs()
        orphan = _change(entity="SH9999")
        corrections = Corrections(
            "shotAnalysis", REV, (_change(), orphan), None, None, None
        )
        result, warnings = apply_corrections([obs], corrections, REV)
        assert result[0].value == "中景"
        assert any("SH9999" in w for w in warnings)

    def test_absent_restriction_warns_instead_of_corrupting(self):
        # 模型来源的观察不能经人工修正变成 absent。
        obs = _obs()
        bad = _change(state="absent", newValue=None)
        corrections = Corrections("shotAnalysis", REV, (bad,), None, None, None)
        result, warnings = apply_corrections([obs], corrections, REV)
        assert result == [obs]
        assert warnings

    def test_boundary_changes_not_applied_here(self):
        obs = _obs()
        boundary = _change(kind="boundary", field="boundary.finalStartMs")
        corrections = Corrections("shotAnalysis", REV, (boundary,), None, None, None)
        result, warnings = apply_corrections([obs], corrections, REV)
        assert result == [obs]
        assert warnings == []


class TestBoundaryChanges:
    def test_extracts_only_boundary_kind(self):
        corrections = Corrections(
            "shotAnalysis",
            REV,
            (_change(), _change(kind="boundary", field="boundary.finalStartMs")),
            None,
            None,
            None,
        )
        extracted = boundary_changes(corrections)
        assert len(extracted) == 1
        assert extracted[0]["kind"] == "boundary"
