"""Phase 2 文本模型编排测试（roadmap 03-03）。

铁律：story prompt 只含文本摘要，绝不发送视频/帧/Data URI；
模型不可用或返回不合规时保留 scaffold，不丢候选 blocks。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memoloupe.analysis.story_pipeline import (
    StoryAnalysisPipeline,
    StoryAnalysisRequest,
    build_scaffold_document,
    build_shot_summaries,
)
from memoloupe.analysis.story_prompts import build_story_prompt
from memoloupe.services.base import PermanentServiceError, TransientServiceError
from memoloupe.services.mock import MockTextModelService
from memoloupe.services.text_model import TextModelRequest

from story_fixtures import (
    asr_doc,
    read_blocks,
    segment,
    shots_doc,
    write_out_dir,
)


def _model_block(block_id: str, **overrides) -> dict:
    block = {
        "storyBlockID": block_id,
        "blockTitle": "出发",
        "divisionAxis": "行动/任务",
        "divisionRationale": "同一行动段落。",
        "primaryRole": "hook",
        "coreContent": "建立旅程起点。",
        "informationRole": "建立背景",
        "narrativeDensity": "中",
        "audienceReaction": "好奇/想看下去",
        "visualIndependence": "静音也能看懂",
        "blockRelation": "铺垫 → 下一块",
        "relationReason": "先建立场景。",
    }
    block.update(overrides)
    return block


def _model_response(block_ids: list[str], slots: list[dict] | None = None) -> str:
    payload = {
        "blocks": [_model_block(bid) for bid in block_ids],
        "slots": slots
        if slots is not None
        else [
            {
                "slotID": "S001",
                "slotType": "开场引入",
                "slotTitle": "开场",
                "blockIDs": block_ids,
                "slotRationale": "全部块构成开场。",
            }
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def _setup(tmp_path: Path) -> Path:
    """两镜头、两段对白（gap>=1200）→ scaffold 两块 B0001/B0002。"""
    return write_out_dir(
        tmp_path / "out",
        shot_ranges=[(0, 3000), (3000, 6000)],
        asr=asr_doc([segment(500, 1500), segment(3000, 5000)]),
    )


def _run(work: Path, service) -> object:
    return StoryAnalysisPipeline().run(
        StoryAnalysisRequest(output_dir=work, text_service=service)
    )


# ---------------------------------------------------------------------------
# prompt：只含文本摘要
# ---------------------------------------------------------------------------


class TestStoryPrompt:
    def _prompt_material(self):
        shots = shots_doc([(0, 3000), (3000, 6000)])
        asr = asr_doc([segment(500, 1500, "出发解说。"), segment(3000, 5000, "到达解说。")])
        unified = {
            "service": "unifiedAudioVideo",
            "status": "complete",
            "clips": [{"shotID": "SH0001", "clipPath": "clips/SH0001.mp4"}],
            "batches": [
                {"response": {"shots": [{
                    "shotID": "SH0001",
                    "visual": {"content": "机场画面", "subjects": "旅客",
                               "actions": "走动", "setting": "机场"},
                    "components": {"texts": [{"textContent": "DAY 1"}]},
                    "editing": {"transition": "硬切"},
                    "evidenceRefs": ["evidence/frames/F_SH0001_MAIN.jpg"],
                }]}}
            ],
        }
        raws = {"shots": shots, "asr": asr, "unified-media": unified}
        summaries = build_shot_summaries(raws)
        scaffold = build_scaffold_document(shots["shots"], asr, 1200)
        return build_story_prompt(summaries, scaffold["blocks"], gap_ms=1200)

    def test_prompt_contains_summaries_and_vocab(self):
        prompt = self._prompt_material()
        assert "机场画面" in prompt
        assert "出发解说。" in prompt
        assert "DAY 1" in prompt
        assert "B0001" in prompt and "B0002" in prompt
        # 受控词表随 prompt 下发。
        assert "建立背景" in prompt
        assert "hook" in prompt

    def test_prompt_has_no_video_or_paths(self):
        prompt = self._prompt_material()
        assert "data:" not in prompt
        assert ".mp4" not in prompt
        assert "clips/" not in prompt
        assert "evidence/frames/" not in prompt
        assert "base64" not in prompt


# ---------------------------------------------------------------------------
# Mock 服务
# ---------------------------------------------------------------------------


class TestMockTextModelService:
    def test_returns_scripted_text(self):
        service = MockTextModelService({0: '{"ok": true}'})
        assert service.generate(TextModelRequest(task="t", prompt="p")) == '{"ok": true}'
        assert len(service.calls) == 1

    def test_raises_scripted_error(self):
        service = MockTextModelService({0: TransientServiceError("HTTP 429")})
        with pytest.raises(TransientServiceError):
            service.generate(TextModelRequest(task="t", prompt="p"))

    def test_unscripted_call_raises(self):
        service = MockTextModelService({})
        with pytest.raises(KeyError):
            service.generate(TextModelRequest(task="t", prompt="p"))


# ---------------------------------------------------------------------------
# 编排：成功与各类降级
# ---------------------------------------------------------------------------


class TestModelFillSuccess:
    def test_success_merges_narrative_and_keeps_deterministic(self, tmp_path):
        work = _setup(tmp_path)
        service = MockTextModelService({0: _model_response(["B0001", "B0002"])})
        report = _run(work, service)
        assert report.status == "complete"
        doc = read_blocks(work)
        assert doc["status"] == "complete"
        assert doc["boundarySource"] == "asr-gap"
        b1, b2 = doc["blocks"]
        # 叙事字段来自模型。
        assert b1["primaryRole"] == "hook"
        assert b1["blockTitle"] == "出发"
        # 确定性字段不被模型覆盖（scaffold 派生）。
        assert b1["shotIDs"] == ["SH0001"]
        assert (b1["startMs"], b1["endMs"]) == (0, 3000)
        assert b2["shotIDs"] == ["SH0002"]
        assert (b2["startMs"], b2["endMs"]) == (3000, 6000)
        assert b1["boundary"]["signal"] == "sourceStart"
        assert b2["boundary"]["signal"] == "asr-gap"
        assert doc["slots"][0]["slotID"] == "S001"
        assert doc["slots"][0]["blockIDs"] == ["B0001", "B0002"]
        # 每次成功请求后 checkpoint。
        assert (work / "checkpoints" / "story-blocks-model.json").is_file()

    def test_checkpoint_reuse_skips_service_call(self, tmp_path):
        work = _setup(tmp_path)
        service = MockTextModelService({0: _model_response(["B0001", "B0002"])})
        _run(work, service)
        assert len(service.calls) == 1
        report = _run(work, service)
        assert report.status == "complete"
        assert len(service.calls) == 1  # checkpoint 命中，不再请求
        assert read_blocks(work)["status"] == "complete"

    def test_fence_wrapped_response_accepted(self, tmp_path):
        work = _setup(tmp_path)
        body = _model_response(["B0001", "B0002"])
        service = MockTextModelService({0: f"```json\n{body}\n```"})
        report = _run(work, service)
        assert report.status == "complete"
        assert read_blocks(work)["status"] == "complete"


class TestModelFillDegradation:
    """不合规/不可用一律保留 scaffold，不丢候选 blocks（docs/03 §3.3）。"""

    def _assert_scaffold_kept(self, work: Path, report) -> None:
        assert report.status == "partial"
        doc = read_blocks(work)
        assert doc["status"] == "scaffold"
        assert [b["storyBlockID"] for b in doc["blocks"]] == ["B0001", "B0002"]
        assert all(b["primaryRole"] == "unknown" for b in doc["blocks"])
        assert doc["slots"] == []

    def test_invalid_json_keeps_scaffold(self, tmp_path):
        work = _setup(tmp_path)
        report = _run(work, MockTextModelService({0: "not json {"}))
        self._assert_scaffold_kept(work, report)

    def test_missing_block_keeps_scaffold(self, tmp_path):
        work = _setup(tmp_path)
        report = _run(work, MockTextModelService({0: _model_response(["B0001"])}))
        self._assert_scaffold_kept(work, report)

    def test_unknown_block_keeps_scaffold(self, tmp_path):
        work = _setup(tmp_path)
        report = _run(
            work, MockTextModelService({0: _model_response(["B0001", "B9999"])})
        )
        self._assert_scaffold_kept(work, report)

    def test_transient_error_keeps_scaffold(self, tmp_path):
        work = _setup(tmp_path)
        report = _run(
            work, MockTextModelService({0: TransientServiceError("HTTP 500")})
        )
        self._assert_scaffold_kept(work, report)

    def test_permanent_error_keeps_scaffold(self, tmp_path):
        work = _setup(tmp_path)
        report = _run(
            work, MockTextModelService({0: PermanentServiceError("HTTP 401")})
        )
        self._assert_scaffold_kept(work, report)

    def test_model_cannot_reorder_or_reassign_shots(self, tmp_path):
        work = _setup(tmp_path)
        bad = _model_response(["B0001", "B0002"])
        payload = json.loads(bad)
        payload["blocks"][0]["shotIDs"] = ["SH0002"]  # 模型试图改 shot 归属
        service = MockTextModelService({0: json.dumps(payload, ensure_ascii=False)})
        report = _run(work, service)
        self._assert_scaffold_kept(work, report)

    def test_model_cannot_change_boundaries(self, tmp_path):
        work = _setup(tmp_path)
        payload = json.loads(_model_response(["B0001", "B0002"]))
        payload["blocks"][0]["endMs"] = 9999
        service = MockTextModelService({0: json.dumps(payload, ensure_ascii=False)})
        report = _run(work, service)
        self._assert_scaffold_kept(work, report)

    def test_slot_block_ids_must_close_over_blocks(self, tmp_path):
        work = _setup(tmp_path)
        slots = [{
            "slotID": "S001", "slotType": "开场引入", "slotTitle": "开场",
            "blockIDs": ["B0001", "B9999"], "slotRationale": "引用未知块",
        }]
        service = MockTextModelService({0: _model_response(["B0001", "B0002"], slots)})
        report = _run(work, service)
        self._assert_scaffold_kept(work, report)

    def test_complete_model_must_assign_every_block_to_a_slot(self, tmp_path):
        work = _setup(tmp_path)
        slots = [{
            "slotID": "S001", "slotType": "开场引入", "slotTitle": "开场",
            "blockIDs": ["B0001"], "slotRationale": "漏掉第二块",
        }]
        service = MockTextModelService({0: _model_response(["B0001", "B0002"], slots)})
        report = _run(work, service)
        self._assert_scaffold_kept(work, report)

    def test_overlong_block_title_rejected(self, tmp_path):
        work = _setup(tmp_path)
        payload = json.loads(_model_response(["B0001", "B0002"]))
        payload["blocks"][0]["blockTitle"] = "这是一个超过十二个字的故事块标题啊"
        service = MockTextModelService({0: json.dumps(payload, ensure_ascii=False)})
        report = _run(work, service)
        self._assert_scaffold_kept(work, report)


class TestControlledVocabularyNormalization:
    def test_enum_whitespace_and_unknown_value(self, tmp_path):
        work = _setup(tmp_path)
        payload = json.loads(_model_response(["B0001", "B0002"]))
        payload["blocks"][0]["divisionAxis"] = " 主题/话题 "  # 归一化去空白
        payload["blocks"][1]["primaryRole"] = "bogus-role"  # 非法 → unknown
        service = MockTextModelService({0: json.dumps(payload, ensure_ascii=False)})
        report = _run(work, service)
        assert report.status == "complete"
        blocks = read_blocks(work)["blocks"]
        assert blocks[0]["divisionAxis"] == "主题/话题"
        assert blocks[1]["primaryRole"] == "unknown"

    def test_information_role_multi_value_filtered(self, tmp_path):
        work = _setup(tmp_path)
        payload = json.loads(_model_response(["B0001", "B0002"]))
        payload["blocks"][0]["informationRole"] = "建立背景、忽悠观众、推进新信息"
        payload["blocks"][1]["informationRole"] = "全是垃圾值"
        service = MockTextModelService({0: json.dumps(payload, ensure_ascii=False)})
        report = _run(work, service)
        assert report.status == "complete"
        blocks = read_blocks(work)["blocks"]
        assert blocks[0]["informationRole"] == "建立背景、推进新信息"
        # 过滤后为空 → unknown 占位（schema 允许的单值）。
        assert blocks[1]["informationRole"] == "unknown"


class TestVocabularyVersionInvalidatesCache:
    """roadmap 05-02：词表版本进入模型填充指纹，词表升级使 checkpoint 失效。"""

    def test_vocab_upgrade_reforwards_model_request(self, tmp_path, monkeypatch):
        work = _setup(tmp_path)
        service = MockTextModelService({0: _model_response(["B0001", "B0002"])})
        first = _run(work, service)
        assert first.status == "complete"
        assert len(service.calls) == 1

        # 词表版本升级（内容变化 → version+1）：指纹变化 → 重发请求。
        import memoloupe.analysis.story_pipeline as sp
        from memoloupe.analysis.vocabulary import Vocabulary

        real = sp.load_vocabulary()
        monkeypatch.setattr(
            sp, "load_vocabulary",
            lambda: Vocabulary(version=real.version + 1, fields=real.fields),
        )
        fresh = MockTextModelService({0: _model_response(["B0001", "B0002"])})
        second = _run(work, fresh)
        assert second.status == "complete"
        assert len(service.calls) == 1  # 旧 mock 未被第二次请求调用
        assert len(fresh.calls) == 1  # 新 mock 收到一次请求：checkpoint 失效。
        assert (work / "checkpoints" / "story-blocks-model.json").is_file()
