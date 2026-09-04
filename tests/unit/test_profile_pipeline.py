"""profile 蒸馏测试（roadmap 04-02）。

铁律：蒸馏 prompt 只含结构化文本，绝不发送视频/帧/Data URI/路径；
确定性字段模型无权覆盖；模型不可用/不合规时保留确定性聚合。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memoloupe.analysis.profile_aggregate import build_profile_aggregate
from memoloupe.analysis.profile_pipeline import (
    ProfileBuildPipeline,
    ProfileBuildRequest,
    parse_profile_distill,
)
from memoloupe.analysis.profile_prompts import (
    distill_prompt_has_no_media,
    build_profile_distill_prompt,
)
from memoloupe.services.base import PermanentServiceError, TransientServiceError
from memoloupe.services.mock import MockTextModelService

from story_fixtures import write_out_dir

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "output_full" / "raw"


def _load_fixture_raw(name: str) -> dict:
    return json.loads((FIXTURE / f"{name}.json").read_text(encoding="utf-8"))


def _raws() -> dict:
    return {
        "media": _load_fixture_raw("media"),
        "shots": _load_fixture_raw("shots"),
        "story-blocks": _load_fixture_raw("story-blocks"),
        "asr": _load_fixture_raw("asr"),
        "audio-cuts": _load_fixture_raw("audio-cuts"),
        "music-flags": _load_fixture_raw("music-flags"),
        "camera-motion": _load_fixture_raw("camera-motion"),
        "unified-media": _load_fixture_raw("unified-media"),
    }


def _aggregate() -> dict:
    return build_profile_aggregate(_raws())


def _story() -> dict:
    return _load_fixture_raw("story-blocks")


def _distill_response() -> str:
    return json.dumps(
        {
            "slots": [
                {
                    "slotId": "S001",
                    "L1": {
                        "functionalTitle": "期待型旅程开场",
                        "narrativeFunction": "setup",
                        "intendedReaction": "好奇/想看下去",
                    },
                    "L2": {
                        "carriage": "出发动线承载",
                        "pattern": "快切建立情绪",
                        "referenceContent": "用机场、行李和出发动作建立旅程期待。",
                    },
                },
                {
                    "slotId": "S002",
                    "L1": {
                        "functionalTitle": "目的地体验兑现",
                        "narrativeFunction": "resolution",
                        "intendedReaction": "共鸣/代入",
                    },
                    "L2": {
                        "carriage": "场景沉浸承载",
                        "pattern": "放缓节奏兑现期待",
                        "referenceContent": "目的地街景兑现前段建立的旅程期待。",
                    },
                },
            ],
            "hook": {
                "L1": {"atSeconds": 0.0, "slotId": "S001", "blockId": "B0001"},
                "L2": {
                    "form": "用出发动作直接制造旅程期待",
                    "referenceContent": "旅行从机场出发。",
                },
                "L3": {"shotIds": ["SH0001", "SH0002"]},
            },
            "payoff": None,
            "structureRequirements": [
                {
                    "slotId": "S001",
                    "requirementType": "evidence",
                    "description": "需要能表达出发或进入旅程状态的素材。",
                    "minEvidence": "至少 2 个可用镜头",
                }
            ],
            "adoptionHints": {
                "strengths": ["用行动而不是旁白快速建立主题"],
                "cautions": ["不要强求用户拥有同一机场或同一地点素材"],
                "suggestedDefault": "L1+L2",
            },
            "discussionItems": [
                {
                    "id": "q-1",
                    "layer": "L2",
                    "category": "applicability",
                    "question": "用户素材里是否有能承担出发/进入状态的镜头？",
                    "options": [
                        {"id": "a", "label": "有明确出发镜头"},
                        {"id": "b", "label": "用抵达或开场环境替代"},
                    ],
                    "impactLevel": "preference",
                    "defaultIfUnanswered": "用最强环境建立镜头替代出发动作",
                }
            ],
        },
        ensure_ascii=False,
    )


class TestDistillPrompt:
    def test_prompt_contains_structured_aggregate(self):
        prompt = build_profile_distill_prompt(_aggregate(), _story())
        assert "S001" in prompt and "S002" in prompt
        assert "期待" not in prompt or "functionalTitle" in prompt  # 主观字段为空
        assert "机场出发" in prompt  # 块叙事摘要（referenceContent 素材）
        assert "narrativeFunction" in prompt
        assert "setup" in prompt

    def test_prompt_has_no_media_or_paths(self):
        prompt = build_profile_distill_prompt(_aggregate(), _story())
        assert distill_prompt_has_no_media(prompt)

    def test_prompt_block_lines_carry_reference_shot_ids(self):
        # hook/payoff.L3.shotIds 是引用而非编造：prompt 必须给出 block→镜头映射
        # （真实链路中 qwen 因缺此信息而返回空数组，见 D-060）。
        prompt = build_profile_distill_prompt(_aggregate(), _story())
        assert "B0001" in prompt
        assert "shots=" in prompt
        assert "SH0001" in prompt
        # 指令必须明确空数组非法、不确定置 null。
        assert "空数组" in prompt and "null" in prompt

    def test_prompt_exempts_hook_l3_reference_from_deterministic_ban(self):
        prompt = build_profile_distill_prompt(_aggregate(), _story())
        # 铁律的 ID 禁令不得误伤 hook 证据引用：两种意图都必须在 prompt 中显式说明。
        assert "不得" in prompt and "L3.shotIds" in prompt


class TestParse:
    def test_valid_response_parses(self):
        result = parse_profile_distill(_distill_response(), _aggregate(), _story())
        assert result["slots"]["S001"]["L1"]["functionalTitle"] == "期待型旅程开场"
        assert result["slots"]["S001"]["L1"]["narrativeFunction"] == "setup"
        assert result["slots"]["S002"]["L2"]["carriage"] == "场景沉浸承载"
        assert result["hook"]["L1"]["slotId"] == "S001"
        assert result["hook"]["L3"]["shotIds"] == ["SH0001", "SH0002"]
        assert result["payoff"] is None
        assert result["structureRequirements"][0]["slotId"] == "S001"
        assert result["adoptionHints"]["suggestedDefault"] == "L1+L2"
        assert result["discussionItems"][0]["id"] == "q-1"
        assert result["discussionItems"][0]["defaultIfUnanswered"]

    def test_fence_wrapped_response_accepted(self):
        body = f"```json\n{_distill_response()}\n```"
        result = parse_profile_distill(body, _aggregate(), _story())
        assert result["slots"]["S001"]["L1"]["narrativeFunction"] == "setup"

    def test_slot_set_mismatch_rejected(self):
        payload = json.loads(_distill_response())
        payload["slots"].pop()
        with pytest.raises(Exception, match="slot 集合"):
            parse_profile_distill(json.dumps(payload), _aggregate(), _story())

    def test_unknown_slot_rejected(self):
        payload = json.loads(_distill_response())
        payload["slots"][0]["slotId"] = "S999"
        with pytest.raises(Exception, match="slot 集合"):
            parse_profile_distill(json.dumps(payload), _aggregate(), _story())

    def test_deterministic_fields_rejected(self):
        payload = json.loads(_distill_response())
        payload["slots"][0]["L1"]["durationShare"] = 0.9
        with pytest.raises(Exception, match="确定性字段"):
            parse_profile_distill(json.dumps(payload), _aggregate(), _story())
        payload2 = json.loads(_distill_response())
        payload2["slots"][0]["L3"] = {"shotIds": ["SH9999"], "shotCount": 1, "avgShotSeconds": 1}
        with pytest.raises(Exception, match="确定性字段"):
            parse_profile_distill(json.dumps(payload2), _aggregate(), _story())

    def test_bad_narrative_function_falls_back_to_null(self):
        payload = json.loads(_distill_response())
        payload["slots"][0]["L1"]["narrativeFunction"] = "bogus-function"
        result = parse_profile_distill(json.dumps(payload), _aggregate(), _story())
        assert result["slots"]["S001"]["L1"]["narrativeFunction"] is None

    def test_hook_unknown_block_rejected(self):
        payload = json.loads(_distill_response())
        payload["hook"]["L1"]["blockId"] = "B9999"
        with pytest.raises(Exception, match="blockId 不存在"):
            parse_profile_distill(json.dumps(payload), _aggregate(), _story())

    def test_hook_unknown_shot_rejected(self):
        payload = json.loads(_distill_response())
        payload["hook"]["L3"]["shotIds"] = ["SH9999"]
        with pytest.raises(Exception, match="shotIds\\[0\\] 不存在"):
            parse_profile_distill(json.dumps(payload), _aggregate(), _story())

    def test_hook_block_must_belong_to_declared_slot(self):
        payload = json.loads(_distill_response())
        payload["hook"]["L1"]["blockId"] = "B0002"
        with pytest.raises(Exception, match="不属于 slotId"):
            parse_profile_distill(json.dumps(payload), _aggregate(), _story())

    def test_hook_shot_must_belong_to_declared_block(self):
        payload = json.loads(_distill_response())
        payload["hook"]["L3"]["shotIds"] = ["SH0003"]
        with pytest.raises(Exception, match="不属于 blockId"):
            parse_profile_distill(json.dumps(payload), _aggregate(), _story())

    def test_hook_empty_shot_ids_rejected(self):
        payload = json.loads(_distill_response())
        payload["hook"]["L3"]["shotIds"] = []
        with pytest.raises(Exception, match="shotIds 必须是非空数组"):
            parse_profile_distill(json.dumps(payload), _aggregate(), _story())

    def test_hook_missing_form_rejected(self):
        payload = json.loads(_distill_response())
        payload["hook"]["L2"]["form"] = ""
        with pytest.raises(Exception, match="form"):
            parse_profile_distill(json.dumps(payload), _aggregate(), _story())

    def test_discussion_items_must_have_default(self):
        payload = json.loads(_distill_response())
        payload["discussionItems"][0]["defaultIfUnanswered"] = ""
        with pytest.raises(Exception, match="defaultIfUnanswered"):
            parse_profile_distill(json.dumps(payload), _aggregate(), _story())

    def test_duplicate_discussion_id_rejected(self):
        payload = json.loads(_distill_response())
        payload["discussionItems"].append(payload["discussionItems"][0])
        with pytest.raises(Exception, match="重复"):
            parse_profile_distill(json.dumps(payload), _aggregate(), _story())

    def test_invalid_json_rejected(self):
        with pytest.raises(Exception, match="非法 JSON"):
            parse_profile_distill("not json {", _aggregate(), _story())


class TestPipeline:
    def _setup(self, tmp_path: Path) -> Path:
        work = write_out_dir(
            tmp_path / "out", shot_ranges=[(0, 3000), (3000, 6000)],
            asr=None, unified=None,
        )
        raw = work / "raw"
        (raw / "story-blocks.json").write_text(
            json.dumps(
                {
                    "status": "complete",
                    "boundarySource": "asr-gap",
                    "gapMs": 1200,
                    "generatedAt": "2026-08-25T00:00:00Z",
                    "blocks": [
                        {
                            "storyBlockID": "B0001",
                            "shotIDs": ["SH0001", "SH0002"],
                            "startMs": 0,
                            "endMs": 6000,
                            "boundary": {"level": "start", "signal": "sourceStart",
                                         "label": "片头"},
                            "divisionAxis": "行动/任务",
                            "divisionRationale": "",
                            "primaryRole": "hook",
                            "coreContent": "建立旅程起点。",
                            "informationRole": "建立背景",
                            "narrativeDensity": "中",
                            "audienceReaction": "好奇/想看下去",
                            "visualIndependence": "静音也能看懂",
                            "blockRelation": "",
                            "relationReason": "",
                        }
                    ],
                    "slots": [
                        {
                            "slotID": "S001",
                            "slotType": "开场引入",
                            "slotTitle": "开场",
                            "blockIDs": ["B0001"],
                            "slotRationale": "全部块构成开场。",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return work

    def _response_for(self, aggregate: dict) -> str:
        slot = aggregate["structure"]["slots"][0]
        return json.dumps(
            {
                "slots": [
                    {
                        "slotId": slot["slotId"],
                        "L1": {"functionalTitle": "开场", "narrativeFunction": "setup",
                               "intendedReaction": "好奇/想看下去"},
                        "L2": {
                            "carriage": "开场承载",
                            "pattern": "快切",
                            "referenceContent": "开场内容。",
                        },
                    }
                ],
                "hook": None,
                "payoff": None,
                "structureRequirements": [],
                "adoptionHints": None,
                "discussionItems": [],
            },
            ensure_ascii=False,
        )

    def test_no_model_produces_aggregate_skipped(self, tmp_path):
        work = self._setup(tmp_path)
        report = ProfileBuildPipeline().run(ProfileBuildRequest(output_dir=work))
        assert report.status == "complete"
        profile = json.loads((work / "style-profile.json").read_text(encoding="utf-8"))
        assert profile["distillStatus"] == "skipped"
        assert profile["structure"]["slots"][0]["L1"]["functionalTitle"] is None

    def test_model_fill_produces_complete(self, tmp_path):
        work = self._setup(tmp_path)
        aggregate = build_profile_aggregate({
            "media": json.loads((work / "raw" / "media.json").read_text(encoding="utf-8")),
            "shots": json.loads((work / "raw" / "shots.json").read_text(encoding="utf-8")),
            "story-blocks": json.loads(
                (work / "raw" / "story-blocks.json").read_text(encoding="utf-8")
            ),
            "asr": None, "audio-cuts": None, "music-flags": None,
            "camera-motion": None, "unified-media": None,
        })
        service = MockTextModelService({0: self._response_for(aggregate)})
        report = ProfileBuildPipeline().run(
            ProfileBuildRequest(output_dir=work, text_service=service)
        )
        assert report.status == "complete"
        profile = json.loads((work / "style-profile.json").read_text(encoding="utf-8"))
        assert profile["distillStatus"] == "complete"
        slot = profile["structure"]["slots"][0]
        assert slot["L1"]["functionalTitle"] == "开场"
        assert slot["L1"]["narrativeFunction"] == "setup"
        assert slot["L2"]["carriage"] == "开场承载"
        # 确定性字段不被模型覆盖。
        assert slot["L3"]["shotCount"] == 2
        assert slot["L1"]["durationShare"] > 0
        assert (work / "checkpoints" / "style-profile-distill.json").is_file()
        # 合并后过 schema。
        from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
        validate_artifact(ArtifactName.STYLE_PROFILE, profile)

    def test_checkpoint_reuse_skips_service_call(self, tmp_path):
        work = self._setup(tmp_path)
        aggregate = build_profile_aggregate({
            "media": json.loads((work / "raw" / "media.json").read_text(encoding="utf-8")),
            "shots": json.loads((work / "raw" / "shots.json").read_text(encoding="utf-8")),
            "story-blocks": json.loads(
                (work / "raw" / "story-blocks.json").read_text(encoding="utf-8")
            ),
            "asr": None, "audio-cuts": None, "music-flags": None,
            "camera-motion": None, "unified-media": None,
        })
        service = MockTextModelService({0: self._response_for(aggregate)})
        request = ProfileBuildRequest(output_dir=work, text_service=service)
        assert ProfileBuildPipeline().run(request).status == "complete"
        assert len(service.calls) == 1
        assert ProfileBuildPipeline().run(request).status == "complete"
        assert len(service.calls) == 1

    @pytest.mark.parametrize("error", [
        TransientServiceError("HTTP 500"),
        PermanentServiceError("HTTP 401"),
    ])
    def test_model_failure_keeps_aggregate(self, tmp_path, error):
        work = self._setup(tmp_path)
        report = ProfileBuildPipeline().run(
            ProfileBuildRequest(
                output_dir=work, text_service=MockTextModelService({0: error})
            )
        )
        assert report.status == "partial"
        profile = json.loads((work / "style-profile.json").read_text(encoding="utf-8"))
        assert profile["distillStatus"] == "skipped"
        assert profile["structure"]["slots"][0]["L1"]["functionalTitle"] is None

    def test_invalid_response_keeps_aggregate(self, tmp_path):
        work = self._setup(tmp_path)
        report = ProfileBuildPipeline().run(
            ProfileBuildRequest(
                output_dir=work,
                text_service=MockTextModelService({0: '{"slots": []}'}),
            )
        )
        assert report.status == "partial"
        profile = json.loads((work / "style-profile.json").read_text(encoding="utf-8"))
        assert profile["distillStatus"] == "skipped"


class TestVocabularyVersionInvalidatesProfileCache(TestPipeline):
    """roadmap 05-02：词表版本进入聚合/蒸馏指纹，词表升级使缓存失效。"""

    def _aggregate_inputs(self, work: Path) -> dict:
        return {
            "media": json.loads((work / "raw" / "media.json").read_text(encoding="utf-8")),
            "shots": json.loads((work / "raw" / "shots.json").read_text(encoding="utf-8")),
            "story-blocks": json.loads((work / "raw" / "story-blocks.json").read_text(encoding="utf-8")),
            "asr": None, "audio-cuts": None, "music-flags": None,
            "camera-motion": None, "unified-media": None,
        }

    def test_vocab_upgrade_forces_aggregate_rebuild(self, tmp_path, monkeypatch):
        from memoloupe.analysis.profile_pipeline import ProfileBuildPipeline as P
        from memoloupe.analysis.profile_aggregate import build_profile_aggregate
        from memoloupe.analysis.vocabulary import Vocabulary

        work = self._setup(tmp_path)
        aggregate = build_profile_aggregate(self._aggregate_inputs(work))
        service = MockTextModelService({0: self._response_for(aggregate)})
        request = ProfileBuildRequest(output_dir=work, text_service=service)
        assert P().run(request).status == "complete"
        first_created = json.loads(
            (work / "style-profile.json").read_text(encoding="utf-8")
        )["createdAt"]

        import memoloupe.analysis.profile_pipeline as pp
        real = pp.load_vocabulary()
        monkeypatch.setattr(
            pp, "load_vocabulary",
            lambda: Vocabulary(version=real.version + 1, fields=real.fields),
        )
        fresh = MockTextModelService({0: self._response_for(aggregate)})
        assert P().run(
            ProfileBuildRequest(output_dir=work, text_service=fresh)
        ).status == "complete"
        assert len(fresh.calls) == 1  # 聚合指纹变化 → 蒸馏 checkpoint 失效
        assert len(service.calls) == 1  # 旧 mock 未再被调用
        second_created = json.loads(
            (work / "style-profile.json").read_text(encoding="utf-8")
        )["createdAt"]
        assert second_created != first_created
