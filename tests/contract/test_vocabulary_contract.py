"""rules/vocabulary.json 契约测试（roadmap 05-02）。

锁定"完整闭集"不变量：

- docs/07 声明的全部受控字段（modelShot 语义字段 + story-blocks 受控集合）
  必须在 vocabulary.json 中登记，值集非空；
- story_prompts 的受控词表常量与 vocabulary.json 对应字段一致（排除
  unknown 占位），防止两份词表漂移；
- 别名合法：目标必须存在于 values（不悬空、不指向别名、不成环），
  casefold 后键不冲突；
- values 不得包含多值分隔符或转换箭头（防止拆分逻辑与词表冲突）；
- 版本为整数，升级词表必须递增版本（缓存失效依据）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memoloupe.analysis.story_prompts import (
    AUDIENCE_REACTIONS,
    DIVISION_AXES,
    INFORMATION_ROLES,
    NARRATIVE_DENSITIES,
    PRIMARY_ROLES,
    SLOT_TYPES,
    VISUAL_INDEPENDENCES,
)
from memoloupe.analysis.vocabulary import TRANSITION_SEPARATOR, load_vocabulary

REPO_ROOT = Path(__file__).resolve().parents[2]
VOCAB_PATH = REPO_ROOT / "rules" / "vocabulary.json"

#: docs/07 §modelShot 声明的受控词表字段（视觉/功能/声音/文字/剪辑）。
MODELSHOT_CONTROLLED_FIELDS: tuple[str, ...] = (
    "visual.framing",
    "visual.cameraAngle",
    "visual.composition",
    "visual.viewpoint",
    "visual.perceivedLensFeel",
    "visual.cameraMovement",
    "visual.brightness",
    "visual.contrast",
    "visual.lightingSource",
    "visual.perceivedColorTemperature",
    "visual.saturation",
    "visual.depthOfField",
    "visual.imageTexture",
    "function.sourceMedium",
    "function.subjectEmotion",
    "function.shotTone",
    "components.texts.textType",
    "components.texts.textAnimation",
)

#: docs/07 §story-blocks 受控集合。
STORY_CONTROLLED_FIELDS: tuple[str, ...] = (
    "divisionAxis",
    "primaryRole",
    "informationRole",
    "narrativeDensity",
    "audienceReaction",
    "visualIndependence",
    "blockRelation",
    "slotType",
)

#: 需要箭头转换能力的字段（docs/07：framing 与 cameraMovement 可用 → 表示变化）。
_TRANSITION_FIELDS = frozenset({"visual.framing", "visual.cameraMovement"})

#: 需要多值分隔的字段。
_MULTI_FIELDS = frozenset({"informationRole", "slotType"})


@pytest.fixture(scope="module")
def vocab():
    return load_vocabulary(VOCAB_PATH)


class TestClosedSet:
    def test_all_modelshot_fields_registered(self, vocab):
        missing = [f for f in MODELSHOT_CONTROLLED_FIELDS if f not in vocab.fields]
        assert missing == []

    def test_all_story_fields_registered(self, vocab):
        missing = [f for f in STORY_CONTROLLED_FIELDS if f not in vocab.fields]
        assert missing == []

    def test_every_field_has_nonempty_values(self, vocab):
        empty = [f for f, rule in vocab.fields.items() if not rule.values]
        assert empty == []

    def test_allow_transitions_config_matches_docs07(self, vocab):
        for field in MODELSHOT_CONTROLLED_FIELDS + STORY_CONTROLLED_FIELDS:
            expected = field in _TRANSITION_FIELDS
            assert vocab.fields[field].allow_transitions is expected, field

    def test_multi_value_separator_config_matches_docs07(self, vocab):
        # docs/07 明确"可多选"的字段必须有顿号分隔符。
        for field in ("informationRole", "slotType"):
            assert vocab.fields[field].multi_value_separator == "、", field

    def test_multi_value_normalization_stays_safe(self, vocab):
        # 允许顿号多值的字段，逐项必须命中词表（整体 unmapped，不半合法）。
        for field, rule in vocab.fields.items():
            if rule.multi_value_separator is None:
                continue
            ok = rule.values[0]
            result = vocab.normalize(field, f"{ok}、不在词表的值")
            assert result.status == "unmapped", field

    def test_values_do_not_contain_separators_or_arrow(self, vocab):
        for field, rule in vocab.fields.items():
            for value in rule.values:
                assert TRANSITION_SEPARATOR not in value, f"{field}: {value!r}"
                assert "、" not in value, f"{field}: {value!r}"

    def test_version_is_positive_integer(self, vocab):
        assert isinstance(vocab.version, int) and vocab.version >= 1


class TestAliasIntegrity:
    def test_alias_targets_exist_in_values(self, vocab):
        for field, rule in vocab.fields.items():
            for alias, target in rule.aliases.items():
                assert target in rule.values, f"{field}: {alias!r} -> {target!r}"

    def test_alias_lookup_is_single_hop(self, vocab):
        # 别名指向的必须是 values 直通项：lookup(alias) 应直接命中目标，
        # 不得出现 alias -> alias（不成环、不级联）。
        for field, rule in vocab.fields.items():
            for alias, target in rule.aliases.items():
                assert rule.lookup(target) == target, f"{field}: {alias!r} -> {target!r} 非直通"
                assert rule.lookup(alias) == target, f"{field}: alias {alias!r} 未命中"

    def test_alias_keys_casefold_unique(self, vocab):
        for field, rule in vocab.fields.items():
            keys = [k.casefold() for k in rule.aliases]
            assert len(keys) == len(set(keys)), f"{field}: 别名键 casefold 冲突"

    def test_alias_not_duplicate_of_value(self, vocab):
        # 别名键不得与 values 重复（冗余且容易漂移）。
        for field, rule in vocab.fields.items():
            values_cf = {v.casefold() for v in rule.values}
            overlap = set(rule.aliases) & values_cf
            assert not overlap, f"{field}: 别名与 values 重复 {sorted(overlap)}"


class TestStoryPromptsConsistency:
    """story_prompts 常量与 vocabulary.json 不得漂移（05-02 单一事实源约束）。"""

    def _assert_equal(self, constants, field, vocab):
        expected = set(constants)
        actual = {v for v in vocab.fields[field].values if v != "unknown"}
        assert expected == actual, (
            f"{field}: story_prompts {sorted(expected)} != vocabulary {sorted(actual)}"
        )

    def test_division_axis(self, vocab):
        self._assert_equal(DIVISION_AXES, "divisionAxis", vocab)

    def test_primary_role(self, vocab):
        self._assert_equal(PRIMARY_ROLES, "primaryRole", vocab)

    def test_information_role(self, vocab):
        self._assert_equal(INFORMATION_ROLES, "informationRole", vocab)

    def test_narrative_density(self, vocab):
        self._assert_equal(NARRATIVE_DENSITIES, "narrativeDensity", vocab)

    def test_audience_reaction(self, vocab):
        self._assert_equal(AUDIENCE_REACTIONS, "audienceReaction", vocab)

    def test_visual_independence(self, vocab):
        self._assert_equal(VISUAL_INDEPENDENCES, "visualIndependence", vocab)

    def test_slot_types(self, vocab):
        self._assert_equal(SLOT_TYPES, "slotType", vocab)

    def test_story_prompts_unknown_only_placeholders(self, vocab):
        # unknown 占位只允许出现在 story scaffold 字段。
        # （docs/07：模型/检测器兜底字段，unknown 是合法枚举值）。
        story_fields = set(STORY_CONTROLLED_FIELDS)
        allowed_unknown = story_fields
        for field, rule in vocab.fields.items():
            has_unknown = "unknown" in rule.values
            if has_unknown:
                assert field in allowed_unknown, f"{field}: 不应含 unknown 占位"


class TestUnmappedMigrationPath:
    """roadmap 05-02：历史 unmapped 的迁移路径。

    unmapped 是运行时归一化结果，不持久化（Observation 每次从 raw 重建）。
    词表升级后，重跑相关阶段（或仅重渲染 HTML）即完成迁移——无需数据迁移。
    本测试用临时词表验证：升级前 unmapped 的值，升级后能归一化为 value。
    """

    def _write_vocab(self, tmp_path: Path, version: int, extra_alias: str | None) -> Path:
        spec = {
            "version": version,
            "fields": {
                "visual.framing": {
                    "values": ["远景", "全景", "中景", "近景", "特写"],
                    "aliases": {"wide shot": "全景"},
                    "allowTransitions": True,
                    "multiValueSeparator": "、",
                }
            },
        }
        if extra_alias is not None:
            spec["fields"]["visual.framing"]["aliases"][extra_alias] = "近景"
        path = tmp_path / "vocabulary.json"
        path.write_text(
            json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return path

    def test_alias_addition_migrates_unmapped_to_value(self, tmp_path):
        v1 = load_vocabulary(self._write_vocab(tmp_path, 1, None))
        assert v1.normalize("visual.framing", "close shot").status == "unmapped"

        v2 = load_vocabulary(self._write_vocab(tmp_path, 2, "close shot"))
        result = v2.normalize("visual.framing", "close shot")
        assert result.status == "value"
        assert result.value == "近景"
        # 版本升级是缓存失效的显式信号（词表内容变化必须递增版本）。
        assert v2.version == v1.version + 1

    def test_unmapped_original_always_preserved(self, tmp_path):
        vocab = load_vocabulary(self._write_vocab(tmp_path, 1, None))
        result = vocab.normalize("visual.framing", "close shot")
        assert result.original == "close shot"
