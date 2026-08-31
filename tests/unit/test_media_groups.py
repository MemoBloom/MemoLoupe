"""analysis.media_groups 单元测试：三组字段所有权、prompt、组 schema、指纹。"""

from __future__ import annotations

import copy

import jsonschema
import pytest

from memoloupe.analysis import media_groups
from memoloupe.analysis.media_groups import (
    GROUP_ORDER,
    build_groups,
    flatten_fields,
    group_schema,
)
from memoloupe.analysis.vocabulary import load_vocabulary
from memoloupe.core.config import load_config
from memoloupe.core.errors import ConfigError
from memoloupe.services.mock import GROUP_OWNED_SECTIONS, _default_shot_fields


@pytest.fixture
def vocab():
    return load_vocabulary()


@pytest.fixture
def config():
    return load_config(env={})


class TestGroupDefinitions:
    def test_three_groups_in_fixed_order(self, vocab, config):
        groups = build_groups(vocab, config)
        assert [g.name for g in groups] == list(GROUP_ORDER)

    def test_field_ownership(self, vocab, config):
        groups = {g.name: g for g in build_groups(vocab, config)}
        visual = groups["visual"].fields
        # v2：模型只描述不可由其它阶段稳定派生的视听语义。
        assert len([f for f in visual if f.startswith("visual.")]) == 18
        assert "visual.content" not in visual
        assert "visual.subjectCoverage" not in visual
        assert "visual.movementIntensity" not in visual
        assert "visual.viewpoint" in visual
        assert "visual.perceivedLensFeel" in visual
        assert "visual.lightingSource" in visual
        assert "visual.perceivedColorTemperature" in visual
        assert "visual.imageTexture" in visual
        assert "components.texts" in visual
        assert "components.nonTextOverlayEvents" in visual
        assert "confidence.visual" in visual
        assert groups["audio"].fields == (
            "audio.bgmStyle",
            "audio.soundEvents",
            "confidence.audio",
        )
        function = groups["function"].fields
        assert "function.sourceMedium" in function
        assert "function.subjectEmotion" in function
        assert "function.shotTone" in function
        assert "editing.transition" not in function
        assert "editing.continuity" not in function
        assert "confidence.function" in function
        assert "confidence.overall" not in function

    def test_no_field_overlap_across_groups(self, vocab, config):
        groups = build_groups(vocab, config)
        seen: dict[str, str] = {}
        for group in groups:
            for path in group.fields:
                assert path not in seen, f"{path} 被 {seen[path]} 与 {group.name} 重复拥有"
                seen[path] = group.name

    def test_overlap_self_check_raises(self, vocab, config, monkeypatch):
        broken = copy.deepcopy(GROUP_OWNED_SECTIONS)
        # 制造真实重叠：audio 组抢走 visual.subjects
        broken["audio"]["visual"] = ("subjects",)
        monkeypatch.setattr(media_groups, "GROUP_OWNED_SECTIONS", broken)
        with pytest.raises(ConfigError):
            build_groups(vocab, config)


class TestPrompt:
    def test_prompt_contains_vocabulary_values(self, vocab, config):
        groups = {g.name: g for g in build_groups(vocab, config)}
        framing_value = vocab.fields["visual.framing"].values[0]
        assert framing_value in groups["visual"].prompt
        tone_value = vocab.fields["function.shotTone"].values[0]
        assert tone_value in groups["function"].prompt
        # 词表版本进入指纹，prompt 中含允许值说明
        assert "允许值" in groups["visual"].prompt

    def test_prompt_contract_clauses(self, vocab, config):
        for group in build_groups(vocab, config):
            assert "shotID 必须原样返回" in group.prompt
            assert "不得遗漏" in group.prompt
            assert "不得返回未请求的镜头" in group.prompt
            assert '"无"' in group.prompt
            assert group.name in group.prompt

    def test_prompt_describes_texts_items(self, vocab, config):
        groups = {g.name: g for g in build_groups(vocab, config)}
        prompt = groups["visual"].prompt
        assert "textContent" in prompt
        assert "textType" in prompt

    def test_prompt_contains_nested_json_template(self, vocab, config):
        groups = {g.name: g for g in build_groups(vocab, config)}
        audio = groups["audio"].prompt
        assert '"audio": {' in audio
        assert '"bgmStyle": "unknown"' in audio
        assert '"confidence": {' in audio
        assert '"audio.bgmStyle"' not in audio.split("JSON 结构模板：", 1)[1]

        function = groups["function"].prompt
        assert '"function": {' in function
        assert '"sourceMedium": "unknown"' in function


class TestGroupSchema:
    def test_schema_shape_and_strictness(self, vocab, config):
        for group in build_groups(vocab, config):
            schema = group.schema
            assert schema["required"] == ["shots"]
            item = schema["properties"]["shots"]["items"]
            assert item["additionalProperties"] is False
            assert item["required"][0] == "shotID"
            owned_sections = set(GROUP_OWNED_SECTIONS[group.name])
            assert set(item["required"][1:]) == owned_sections
            assert set(item["properties"]) == {"shotID", *owned_sections}

    def test_default_mock_payload_passes_group_schema(self, vocab, config):
        for group in build_groups(vocab, config):
            payload = {"shots": [_default_shot_fields(group.name, "SH0001")]}
            jsonschema.validate(payload, group.schema)

    def test_foreign_section_rejected(self, vocab, config):
        schema = group_schema("audio")
        shot = _default_shot_fields("audio", "SH0001")
        shot["visual"] = {"content": "越权字段"}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"shots": [shot]}, schema)

    def test_confidence_enum_restricted(self, vocab, config):
        schema = group_schema("audio")
        shot = _default_shot_fields("audio", "SH0001")
        shot["confidence"]["audio"] = "very-high"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"shots": [shot]}, schema)


class TestFingerprint:
    def test_deterministic(self, vocab, config):
        first = build_groups(vocab, config)
        second = build_groups(vocab, config)
        assert [g.fingerprint for g in first] == [g.fingerprint for g in second]

    def test_groups_have_distinct_fingerprints(self, vocab, config):
        fps = [g.fingerprint for g in build_groups(vocab, config)]
        assert len(set(fps)) == 3

    def test_flatten_fields_dotted_paths(self):
        sections = {"visual": ("subjects", "framing"), "confidence": ("visual",)}
        assert flatten_fields(sections) == (
            "visual.subjects",
            "visual.framing",
            "confidence.visual",
        )
