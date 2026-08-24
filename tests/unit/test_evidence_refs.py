"""evidence_refs 模块单元测试（覆盖 docs/02 §2 全部示例）。"""

from __future__ import annotations

import pytest

from memoloupe.core.errors import EvidenceRefError
from memoloupe.core.evidence_refs import (
    EvidenceRef,
    parse_evidence_ref,
    resolve_json_pointer,
)


class TestParseEvidenceRef:
    @pytest.mark.parametrize(
        ("ref", "file_path", "pointer"),
        [
            ("raw/media.json#source.durationMs", "raw/media.json", "source.durationMs"),
            ("raw/shots.json#shots[0]", "raw/shots.json", "shots[0]"),
            (
                "raw/unified-media.json#batches[0].response.shots[0].visual.framing",
                "raw/unified-media.json",
                "batches[0].response.shots[0].visual.framing",
            ),
        ],
    )
    def test_json_refs_from_spec(self, ref: str, file_path: str, pointer: str) -> None:
        parsed = parse_evidence_ref(ref)
        assert parsed == EvidenceRef(
            file_path=file_path, json_pointer=pointer, is_file_evidence=False
        )

    @pytest.mark.parametrize(
        "ref",
        ["clips/SH0001.mp4", "evidence/frames/F_SH0001_MAIN.jpg"],
    )
    def test_file_evidence_from_spec(self, ref: str) -> None:
        parsed = parse_evidence_ref(ref)
        assert parsed == EvidenceRef(
            file_path=ref, json_pointer=None, is_file_evidence=True
        )

    @pytest.mark.parametrize(
        "ref",
        [
            "/abs/path.json#x",  # 绝对路径
            "/abs/path.mp4",
            "~/home/file.mp4",  # ~ 开头
            "raw/../secret.json#x",  # .. 逃逸
            "..\\up\\file.mp4",
            "raw\\shots.json#shots[0]",  # 反斜杠
            "raw/shots.json#",  # 空指针
            "",  # 空引用
            "#shots[0]",  # 空文件路径
        ],
    )
    def test_rejected(self, ref: str) -> None:
        with pytest.raises(EvidenceRefError):
            parse_evidence_ref(ref)


class TestResolveJsonPointer:
    DOC = {
        "source": {"durationMs": 60064},
        "shots": [{"shotID": "SH0001"}, {"shotID": "SH0002"}],
        "batches": [
            {
                "response": {
                    "shots": [{"visual": {"framing": "全景"}}],
                }
            }
        ],
    }

    def test_simple_key(self) -> None:
        assert resolve_json_pointer(self.DOC, "source.durationMs") == 60064

    def test_array_index(self) -> None:
        assert resolve_json_pointer(self.DOC, "shots[0]") == {"shotID": "SH0001"}
        assert resolve_json_pointer(self.DOC, "shots[1].shotID") == "SH0002"

    def test_deep_mixed(self) -> None:
        assert (
            resolve_json_pointer(
                self.DOC, "batches[0].response.shots[0].visual.framing"
            )
            == "全景"
        )

    def test_missing_key_raises(self) -> None:
        with pytest.raises(EvidenceRefError):
            resolve_json_pointer(self.DOC, "source.missing")

    def test_index_out_of_range_raises(self) -> None:
        with pytest.raises(EvidenceRefError):
            resolve_json_pointer(self.DOC, "shots[9]")

    def test_index_on_non_list_raises(self) -> None:
        with pytest.raises(EvidenceRefError):
            resolve_json_pointer(self.DOC, "source[0]")

    def test_key_on_non_dict_raises(self) -> None:
        with pytest.raises(EvidenceRefError):
            resolve_json_pointer(self.DOC, "source.durationMs.more")

    def test_error_carries_path(self) -> None:
        with pytest.raises(EvidenceRefError) as exc_info:
            resolve_json_pointer(self.DOC, "shots[9]")
        assert exc_info.value.path == "shots"

    def test_invalid_pointer_segment(self) -> None:
        with pytest.raises(EvidenceRefError):
            resolve_json_pointer(self.DOC, "shots[x]")
