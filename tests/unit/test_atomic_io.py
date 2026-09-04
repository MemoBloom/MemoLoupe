"""atomic_io 模块单元测试。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from memoloupe.core.atomic_io import read_json, write_json_atomic, write_text_atomic
from memoloupe.core.errors import ContractError


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    value = {"b": 1, "a": {"nested": [1, 2, 3]}}
    write_json_atomic(path, value)
    assert read_json(path) == value


def test_atomic_replace_overwrites_old_content(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    write_json_atomic(path, {"old": True})
    write_json_atomic(path, {"new": True})
    assert read_json(path) == {"new": True}


def test_chinese_not_escaped(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    write_json_atomic(path, {"framing": "全景"})
    raw = path.read_text(encoding="utf-8")
    assert "全景" in raw
    assert "\\u" not in raw


def test_trailing_newline(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    write_json_atomic(path, {"a": 1})
    assert path.read_bytes().endswith(b"\n")


def test_keys_sorted_stably(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    write_json_atomic(path, {"z": 1, "m": 2, "a": 3})
    text = path.read_text(encoding="utf-8")
    assert text.index('"a"') < text.index('"m"') < text.index('"z"')
    # 与标准 json.dumps 的固定序列化规则一致
    assert text == json.dumps(
        {"z": 1, "m": 2, "a": 3}, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"


def test_failed_replace_preserves_original_and_no_temp_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "a.json"
    write_json_atomic(path, {"old": True})
    original_bytes = path.read_bytes()

    def boom(src: object, dst: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        write_json_atomic(path, {"new": True})

    assert path.read_bytes() == original_bytes
    leftovers = [p for p in tmp_path.iterdir() if p.name != "a.json"]
    assert leftovers == []


def test_read_json_invalid_raises_contract_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ContractError):
        read_json(path)


def test_read_json_missing_file_raises_contract_error(tmp_path: Path) -> None:
    with pytest.raises(ContractError):
        read_json(tmp_path / "missing.json")


def test_read_json_non_object_raises_contract_error(tmp_path: Path) -> None:
    path = tmp_path / "list.json"
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ContractError):
        read_json(path)


def test_write_text_atomic(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    write_text_atomic(path, "你好，拉片")
    assert path.read_text(encoding="utf-8") == "你好，拉片"


def test_write_text_atomic_failure_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "note.txt"
    write_text_atomic(path, "旧内容")

    def boom(src: object, dst: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        write_text_atomic(path, "新内容")
    assert path.read_text(encoding="utf-8") == "旧内容"
    assert [p for p in tmp_path.iterdir() if p.name != "note.txt"] == []
