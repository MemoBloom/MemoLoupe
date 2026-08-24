"""errors 模块单元测试。"""

from __future__ import annotations

import pytest

from memoloupe.core.errors import (
    ArtifactError,
    CapabilityUnavailableError,
    ConfigError,
    ContractError,
    EvidenceRefError,
    MemoLoupeError,
)


def test_all_errors_inherit_base() -> None:
    for cls in (
        ContractError,
        ConfigError,
        CapabilityUnavailableError,
        EvidenceRefError,
        ArtifactError,
    ):
        assert issubclass(cls, MemoLoupeError)


def test_contract_error_str_has_full_location() -> None:
    err = ContractError(
        artifact="shots",
        json_path="$.shots[0].shotID",
        expected="SH + 4 位数字",
        actual="XX0001",
        message="镜头 ID 非法",
    )
    text = str(err)
    assert "shots" in text
    assert "$.shots[0].shotID" in text
    assert "SH + 4 位数字" in text
    assert "XX0001" in text
    assert "镜头 ID 非法" in text
    assert err.artifact == "shots"
    assert err.json_path == "$.shots[0].shotID"


def test_contract_error_without_message() -> None:
    err = ContractError("media", "$.source.durationMs", "int", "null")
    text = str(err)
    assert "artifact=media" in text
    assert "path=$.source.durationMs" in text
    assert "message=" not in text


def test_capability_unavailable_carries_capability() -> None:
    err = CapabilityUnavailableError("appleVision", "helper 不存在")
    assert err.capability == "appleVision"
    assert "appleVision" in str(err)
    assert "helper 不存在" in str(err)


def test_evidence_ref_error_carries_ref_and_path() -> None:
    err = EvidenceRefError("raw/shots.json#shots[9]", "数组下标越界: [9]", path="shots")
    assert err.ref == "raw/shots.json#shots[9]"
    assert err.path == "shots"
    assert "raw/shots.json#shots[9]" in str(err)


def test_artifact_error_str() -> None:
    err = ArtifactError("shots", "写入失败")
    assert "shots" in str(err)
    assert "写入失败" in str(err)


def test_config_error_is_catchable_as_base() -> None:
    with pytest.raises(MemoLoupeError):
        raise ConfigError("bad config")
