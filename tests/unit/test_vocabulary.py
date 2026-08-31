"""tests/unit/test_vocabulary.py — rules/vocabulary.json 与 Vocabulary 归一化。"""

from pathlib import Path

import pytest

from memoloupe.analysis.vocabulary import (
    Vocabulary,
    load_vocabulary,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VOCAB_PATH = REPO_ROOT / "rules" / "vocabulary.json"


@pytest.fixture(scope="module")
def vocab() -> Vocabulary:
    return load_vocabulary(VOCAB_PATH)


class TestLoad:
    def test_default_load_reads_repo_rules(self):
        vocab = load_vocabulary()
        assert vocab.version >= 2
        assert "visual.framing" in vocab.fields

    def test_explicit_path_load(self, vocab):
        assert "divisionAxis" in vocab.fields
        assert "slotType" in vocab.fields

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_vocabulary(tmp_path / "nope.json")


class TestNormalizeDirectHit:
    def test_exact_value(self, vocab):
        r = vocab.normalize("visual.framing", "全景")
        assert r.status == "value"
        assert r.value == "全景"
        assert r.original == "全景"

    def test_alias_hit(self, vocab):
        r = vocab.normalize("visual.framing", "wide shot")
        assert r.status == "value"
        assert r.value == "全景"

    def test_alias_case_insensitive_and_stripped(self, vocab):
        r = vocab.normalize("visual.framing", "  Wide Shot ")
        assert r.status == "value"
        assert r.value == "全景"

    def test_english_enum_value_hit(self, vocab):
        r = vocab.normalize("primaryRole", "hook")
        assert r.status == "value"
        assert r.value == "hook"

    def test_value_with_whitespace_is_stripped(self, vocab):
        r = vocab.normalize("visual.cameraAngle", " 平视 ")
        assert r.status == "value"
        assert r.value == "平视"


class TestNormalizeTransitions:
    def test_transition_all_hit(self, vocab):
        r = vocab.normalize("visual.framing", "远景 → 中景")
        assert r.status == "value"
        assert r.value == "远景 → 中景"

    def test_transition_with_alias_segment(self, vocab):
        r = vocab.normalize("visual.framing", "wide shot → 特写")
        assert r.status == "value"
        assert r.value == "全景 → 特写"

    def test_transition_partial_miss_is_unmapped(self, vocab):
        r = vocab.normalize("visual.framing", "远景 → 航拍")
        assert r.status == "unmapped"
        assert r.value is None
        assert r.original == "远景 → 航拍"

    def test_transition_not_allowed_field_is_unmapped(self, vocab):
        # composition 不允许转换，含箭头的自由文本不得绕过词表
        r = vocab.normalize("visual.composition", "居中 → 对称")
        assert r.status == "unmapped"
        assert r.original == "居中 → 对称"

    def test_camera_movement_transition(self, vocab):
        r = vocab.normalize("visual.cameraMovement", "推 → 跟")
        assert r.status == "value"
        assert r.value == "推 → 跟"


class TestNormalizeMultiValue:
    def test_multi_value_all_hit(self, vocab):
        r = vocab.normalize("informationRole", "建立背景、推进新信息")
        assert r.status == "value"
        assert r.value == "建立背景、推进新信息"

    def test_multi_value_partial_miss_is_unmapped(self, vocab):
        r = vocab.normalize("informationRole", "建立背景、煽情")
        assert r.status == "unmapped"
        assert r.original == "建立背景、煽情"

    def test_slot_type_multi_value(self, vocab):
        r = vocab.normalize("slotType", "开场引入、背景铺垫")
        assert r.status == "value"
        assert r.value == "开场引入、背景铺垫"


class TestNormalizeUnknownAndUnmapped:
    @pytest.mark.parametrize("raw", [None, "", "   ", "unknown", "UNKNOWN", "Unknown"])
    def test_unknown_inputs(self, vocab, raw):
        r = vocab.normalize("visual.framing", raw)
        assert r.status == "unknown"
        assert r.value is None
        assert r.original == raw

    def test_unknown_field_is_unknown_not_error(self, vocab):
        r = vocab.normalize("visual.dominantColor", "蓝白")
        assert r.status == "unknown"
        assert r.original == "蓝白"

    def test_unmapped_preserves_original(self, vocab):
        r = vocab.normalize("visual.framing", "航拍")
        assert r.status == "unmapped"
        assert r.value is None
        assert r.original == "航拍"

    def test_non_string_raw_is_unmapped(self, vocab):
        r = vocab.normalize("visual.framing", 42)
        assert r.status == "unmapped"
        assert r.original == 42


class TestCanonicalKey:
    def test_direct_value(self, vocab):
        assert vocab.canonical_key("visual.framing", "中景") == "中景"

    def test_alias_maps_to_canonical(self, vocab):
        assert vocab.canonical_key("visual.framing", "wide shot") == "全景"

    def test_transition_value(self, vocab):
        assert vocab.canonical_key("visual.framing", "远景 → 中景") == "远景 → 中景"

    def test_miss_returns_none(self, vocab):
        assert vocab.canonical_key("visual.framing", "航拍") is None

    def test_unknown_field_returns_none(self, vocab):
        assert vocab.canonical_key("visual.dominantColor", "蓝白") is None


class TestPromptFragment:
    def test_lists_allowed_values(self, vocab):
        text = vocab.prompt_fragment("visual.framing")
        assert "visual.framing" in text
        for v in ["远景", "全景", "中景", "近景", "特写"]:
            assert v in text

    def test_transition_hint(self, vocab):
        text = vocab.prompt_fragment("visual.framing")
        assert "→" in text

    def test_multi_value_hint(self, vocab):
        text = vocab.prompt_fragment("informationRole")
        assert "、" in text

    def test_unknown_field_fragment(self, vocab):
        text = vocab.prompt_fragment("visual.dominantColor")
        assert "visual.dominantColor" in text
