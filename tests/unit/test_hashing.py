"""hashing 模块单元测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from memoloupe.core.hashing import content_revision_id, fingerprint


def test_content_revision_id_known_vector(tmp_path: Path) -> None:
    path = tmp_path / "abc.bin"
    path.write_bytes(b"abc")
    expected = hashlib.sha256(b"abc").hexdigest()[:12]
    assert content_revision_id(path) == expected
    # 已知向量：sha256("abc") 以 ba7816bf8f01 开头
    assert content_revision_id(path) == "ba7816bf8f01"


def test_content_revision_id_streams_large_file(tmp_path: Path) -> None:
    path = tmp_path / "big.bin"
    data = b"x" * (3 * 1024 * 1024 + 7)
    path.write_bytes(data)
    assert content_revision_id(path) == hashlib.sha256(data).hexdigest()[:12]


def test_fingerprint_known_vector() -> None:
    # 规范化后的串为 {"a":1}
    expected = hashlib.sha256(b'{"a":1}').hexdigest()[:16]
    assert fingerprint({"a": 1}) == expected


def test_fingerprint_same_input_same_result() -> None:
    parts = {"shots": {"minimumFrames": 8}, "nested": [1, 2]}
    assert fingerprint(parts) == fingerprint(parts)


def test_fingerprint_key_order_irrelevant() -> None:
    a = {"a": 1, "b": {"x": 1, "y": 2}}
    b = {"b": {"y": 2, "x": 1}, "a": 1}
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_chinese_stable() -> None:
    a = fingerprint({"值": "全景"})
    b = fingerprint({"值": "全景"})
    assert a == b
    assert fingerprint({"值": "全景"}) != fingerprint({"值": "中景"})
