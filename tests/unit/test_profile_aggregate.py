"""analysis.profile_aggregate 单元测试（roadmap 04-01）。

覆盖：slot 结构（L1/L3）、durationShare/rangeSeconds/minBlocks、pacing
（shotDuration/densityCurve/slotPacing/audioBoundaryBySlot/musicAlignment）、
style 分布与 coverage、expectationChains、asrTextStats、source、
降级路径（unified/asr/music 缺失）、schema 合法性、纯函数不写盘。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memoloupe.analysis.profile_aggregate import (
    PROFILE_AGGREGATE_VERSION,
    SCHEMA_VERSION,
    build_profile_aggregate,
    _asr_text_stats,
    _expectation_chains,
    _music_alignment,
)
from memoloupe.analysis.vocabulary import FieldRule, Vocabulary
from memoloupe.artifacts.schemas import ArtifactName, validate_artifact

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "output_full" / "raw"

#: 加载 fixture 全部 raws（缺失容忍）。
def _fixture_raws(*names: str) -> dict:
    raws: dict = {}
    for name in names:
        path = FIXTURE / f"{name}.json"
        raws[name] = (
            json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        )
    return raws


def _full_raws() -> dict:
    return _fixture_raws(
        "media", "shots", "asr", "music-flags", "audio-cuts",
        "camera-motion", "unified-media", "story-blocks",
    )


def _aggregate(**overrides) -> dict:
    raws = _full_raws()
    raws.update(overrides)
    return build_profile_aggregate(raws)


class TestSlotStructure:
    def test_slots_match_story_slots(self):
        doc = _aggregate()
        assert [s["slotId"] for s in doc["structure"]["slots"]] == ["S001", "S002"]
        s1 = doc["structure"]["slots"][0]
        assert s1["L1"]["types"] == ["开场引入"]
        assert s1["L1"]["durationShare"] == pytest.approx(6400 / 9800, rel=1e-4)
        assert s1["L1"]["rangeSeconds"] == [0.0, 6.4]
        assert s1["L1"]["minBlocks"] == 1
        # 模型主观字段：确定性聚合输出合法占位。
        assert s1["L1"]["functionalTitle"] is None
        assert s1["L1"]["narrativeFunction"] is None
        assert s1["L1"]["intendedReaction"] is None
        assert s1["L2"]["carriage"] is None
        assert s1["L2"]["pattern"] is None
        assert s1["L2"]["referenceContent"] == ""
        assert s1["L3"]["shotIds"] == ["SH0001", "SH0002"]
        assert s1["L3"]["shotCount"] == 2
        assert s1["L3"]["avgShotSeconds"] == pytest.approx(3.2, abs=0.001)

    def test_slot_types_multi_value_split(self):
        story = _fixture_raws("story-blocks")["story-blocks"]
        story["slots"][0]["slotType"] = "开场引入、背景铺垫"
        doc = _aggregate(**{"story-blocks": story})
        assert doc["structure"]["slots"][0]["L1"]["types"] == ["开场引入", "背景铺垫"]

    def test_scaffold_empty_slots(self):
        story = _fixture_raws("story-blocks")["story-blocks"]
        story["slots"] = []
        doc = _aggregate(**{"story-blocks": story})
        assert doc["structure"]["slots"] == []

    def test_schema_valid(self):
        validate_artifact(ArtifactName.STYLE_PROFILE, _aggregate())

    def test_version_constants(self):
        assert SCHEMA_VERSION == 2
        assert PROFILE_AGGREGATE_VERSION.startswith("profile-aggregate.")


class TestPacing:
    def test_shot_duration_stats(self):
        doc = _aggregate()
        # 3 镜头 durations [3.203, 3.197, 3.4]
        assert doc["pacing"]["shotDuration"]["mean"] == pytest.approx(3.2667, abs=0.001)
        assert doc["pacing"]["shotDuration"]["p50"] == pytest.approx(3.203, abs=0.001)
        assert "p10" not in doc["pacing"]["shotDuration"]  # 镜头 < 5 不附 p10/p90

    def test_density_curve_from_blocks(self):
        doc = _aggregate()
        by_slot = {e["slotId"]: e["density"] for e in doc["pacing"]["densityCurve"]}
        assert by_slot == {"S001": "中", "S002": "高"}

    def test_density_all_unknown_is_unknown(self):
        story = _fixture_raws("story-blocks")["story-blocks"]
        for block in story["blocks"]:
            block["narrativeDensity"] = "unknown"
        doc = _aggregate(**{"story-blocks": story})
        assert doc["pacing"]["densityCurve"][0]["density"] == "unknown"

    def test_slot_pacing(self):
        doc = _aggregate()
        by_slot = {e["slotId"]: e for e in doc["pacing"]["slotPacing"]}
        assert by_slot["S001"]["shotCount"] == 2
        assert by_slot["S001"]["avgShotSeconds"] == pytest.approx(3.2, abs=0.001)
        assert by_slot["S002"]["shotCount"] == 1

    def test_audio_boundary_aligned_when_synced(self):
        # fixture 两块之间（SH0002 boundaryOut）是 synchronizedCut。
        doc = _aggregate()
        by_slot = {e["slotId"]: e["boundaryAligned"] for e in doc["pacing"]["audioBoundaryBySlot"]}
        assert by_slot == {"S001": True, "S002": True}

    def test_audio_boundary_not_aligned_when_no_audio_cuts(self):
        # 构造一个 slot 含两块（有内部接缝）：
        # 无 audio-cuts → 接缝不可判定 → false。
        story = _fixture_raws("story-blocks")["story-blocks"]
        story["slots"] = [
            {
                "slotID": "S001",
                "slotType": "开场引入",
                "slotTitle": "开场",
                "blockIDs": ["B0001", "B0002"],
                "slotRationale": "两块合一槽。",
            }
        ]
        doc = _aggregate(**{"story-blocks": story, "audio-cuts": None})
        by_slot = {e["slotId"]: e["boundaryAligned"] for e in doc["pacing"]["audioBoundaryBySlot"]}
        assert by_slot == {"S001": False}

    def test_music_alignment_signals(self):
        music = _fixture_raws("music-flags")["music-flags"]
        story = _fixture_raws("story-blocks")["story-blocks"]
        blocks = story["blocks"]
        # 区间 [0,10) 覆盖全部内部边界 → aligned
        music["musicIntervals"] = [{"startSec": 0.0, "endSec": 10.0}]
        assert _music_alignment(blocks, music) == "music aligned with story boundaries"
        # 区间 [0,5) 不覆盖 6.4s 边界 → not aligned
        music["musicIntervals"] = [{"startSec": 0.0, "endSec": 5.0}]
        assert _music_alignment(blocks, music) == "music not aligned with story boundaries"
        # 无音乐 → no music detected
        music["musicIntervals"] = []
        assert _music_alignment(blocks, music) == "no music detected"
        assert _music_alignment(blocks, None) == "no music detected"

    def test_music_alignment_unknown_without_internal_boundary(self):
        story = _fixture_raws("story-blocks")["story-blocks"]
        blocks = story["blocks"]
        music = {"status": "complete", "musicIntervals": [{"startSec": 0.0, "endSec": 5.0}]}
        assert _music_alignment([blocks[0]], music) == "unknown"


class TestStyle:
    def test_distributions_by_shot_count(self):
        doc = _aggregate()
        # 三镜头 unified 全部相同值 → 单一键 1.0。
        assert doc["style"]["transitions"] == {"硬切": 1.0}
        assert doc["style"]["framing"] == {"全景": 1.0}
        assert doc["style"]["lighting"] == {"自然光": 1.0}
        # cameraMovement 来自 camera-motion.json 确定性值（保留原始枚举）。
        cm = doc["style"]["cameraMovement"]
        # 分布逐值四舍五入，总和允许舍入容差（docs/02 §4.12）。
        assert sum(cm.values()) == pytest.approx(1.0, abs=1e-3)
        assert set(cm) <= {
            "pan_right", "pan_left", "tilt_up", "tilt_down", "zoom_in",
            "zoom_out", "roll", "static", "handheld", "discontinuity",
        }

    def test_coverage_by_time_union(self):
        doc = _aggregate()
        # textOverlay：3 镜头都有文字 → 1.0
        assert doc["style"]["textOverlay"] == {"coverage": 1.0}
        # bgm：按 musicIntervals 时间并集 → 3200/9800
        assert doc["style"]["bgm"]["coverage"] == pytest.approx(3200 / 9800, rel=1e-3)
        # voiceMix：speech 3340ms / 9800
        assert doc["style"]["voiceMix"]["speechCoverage"] == pytest.approx(3340 / 9800, rel=1e-3)
        # hosted：fixture subjects="旅行者" 命中 HOSTED_KEYWORDS → 1.0
        assert doc["style"]["hostedCoverage"] == pytest.approx(1.0, abs=1e-4)

    def test_speech_coverage_uses_segment_union_not_sum(self):
        asr = _fixture_raws("asr")["asr"]
        asr["transcript"]["segments"] = [
            {"startMs": 0, "endMs": 7000, "text": "第一段"},
            {"startMs": 3000, "endMs": 9800, "text": "重叠段"},
        ]
        doc = _aggregate(asr=asr)
        assert doc["style"]["voiceMix"]["speechCoverage"] == 1.0
        validate_artifact(ArtifactName.STYLE_PROFILE, doc)

    def test_bgm_coverage_prefers_music_interval_union(self):
        music = _fixture_raws("music-flags")["music-flags"]
        music["musicIntervals"] = [
            {"startSec": 0.0, "endSec": 5.0},
            {"startSec": 2.0, "endSec": 9.8},
        ]
        for entry in music["shots"]:
            entry["musicOverlapRatio"] = 0.0
        doc = _aggregate(**{"music-flags": music})
        assert doc["style"]["bgm"]["coverage"] == 1.0
        validate_artifact(ArtifactName.STYLE_PROFILE, doc)

    def test_hosted_coverage_conservative_when_no_keyword(self):
        unified = _fixture_raws("unified-media")["unified-media"]
        for batch in unified["batches"]:
            for shot in batch["response"]["shots"]:
                shot["visual"]["subjects"] = "街景与招牌"
        doc = _aggregate(**{"unified-media": unified})
        assert doc["style"]["hostedCoverage"] == 0.0

    def test_missing_unified_yields_empty_distributions(self):
        doc = _aggregate(**{"unified-media": None})
        # transitions 来自 shots 边界，不依赖模型。
        assert doc["style"]["transitions"] == {"硬切": 1.0}
        assert doc["style"]["framing"] == {}
        assert doc["style"]["lighting"] == {}
        assert doc["style"]["textOverlay"] == {}
        assert doc["style"]["hostedCoverage"] == 0.0
        # cameraMovement 仍来自确定性 camera-motion。
        assert doc["style"]["cameraMovement"]

    def test_missing_music_yields_empty_bgm(self):
        doc = _aggregate(**{"music-flags": None})
        assert doc["style"]["bgm"] == {}
        assert doc["pacing"]["musicAlignment"] == "no music detected"

    def test_missing_camera_yields_empty_camera_distribution(self):
        doc = _aggregate(**{"camera-motion": None})
        assert doc["style"]["cameraMovement"] == {}

    def test_unknown_values_excluded_from_distribution(self):
        shots = _fixture_raws("shots")["shots"]
        shots["shots"][0]["boundaryIn"]["type"] = "sourceStart"
        doc = _aggregate(shots=shots)
        assert doc["style"]["transitions"] == {"硬切": 1.0}

    def test_distribution_uses_supplied_vocabulary(self):
        unified = _fixture_raws("unified-media")["unified-media"]
        for batch in unified["batches"]:
            for shot in batch["response"]["shots"]:
                shot["visual"]["framing"] = "wide shot"
        custom = Vocabulary(
            version=999,
            fields={
                "visual.framing": FieldRule(
                    values=("远景", "全景"),
                    aliases={"wide shot": "远景"},
                ),
                "visual.lightingSource": FieldRule(values=("自然光",), aliases={}),
            },
        )
        doc = build_profile_aggregate(
            {**_full_raws(), "unified-media": unified},
            vocabulary=custom,
        )
        assert doc["style"]["framing"] == {"远景": 1.0}


class TestStructureExtras:
    def test_expectation_chains_from_block_relation(self):
        chains = _aggregate()["structure"]["expectationChains"]
        assert chains == [
            {
                "kind": "铺垫",
                "fromSlot": "S001",
                "toSlot": "S002",
                "evidence": {
                    "blockId": "B0001",
                    "relation": "铺垫 → B0002",
                },
            }
        ]

    def test_internal_slot_relation_not_a_chain(self):
        story = _fixture_raws("story-blocks")["story-blocks"]
        story["blocks"][0]["blockRelation"] = "铺垫 → B0001"  # 指向自身
        assert _expectation_chains(story, {b["storyBlockID"]: b for b in story["blocks"]}) == []

    def test_turns_and_nonlinear_conservative(self):
        doc = _aggregate()
        assert doc["structure"]["turns"] == []
        assert doc["structure"]["nonLinearDevices"] == []
        assert doc["structure"]["hook"] is None
        assert doc["structure"]["payoff"] is None

    def test_model_suggestion_fields_conservative(self):
        doc = _aggregate()
        assert doc["structureRequirements"] == []
        assert doc["adoptionHints"] is None
        assert doc["discussionItems"] == []
        assert doc["distillStatus"] == "skipped"


class TestAsrStatsAndSource:
    def test_asr_text_stats(self):
        doc = _aggregate()
        assert doc["asrTextStats"] == {
            "segmentCount": 2,
            "characterCount": len("今天我们从机场出发。") + len("第二段解说。"),
            "speechDurationMs": (2460 - 820) + (5200 - 3500),
        }

    def test_asr_stats_empty_when_missing(self):
        assert _asr_text_stats(None) == {
            "segmentCount": 0, "characterCount": 0, "speechDurationMs": 0,
        }
        doc = _aggregate(**{"asr": None})
        assert doc["asrTextStats"]["segmentCount"] == 0
        assert doc["style"]["voiceMix"] == {}

    def test_source_from_media(self):
        doc = _aggregate()
        source = doc["source"]
        assert source["videoTitle"] == "travel-reference"  # fixture assetID
        assert source["videoPath"] == "/Users/me/Videos/travel-reference.mp4"
        assert source["durationSeconds"] == pytest.approx(9.8, abs=0.001)
        assert source["sourceRevision"] == "a1b2c3d4e5f6"
        assert source["shotAnalysisPath"] == "shot-analysis.html"
        assert source["storyAnalysisPath"] == "shot-analysis.html"
        assert doc["id"] == "profile-a1b2c3d4e5f6"
        assert doc["schemaVersion"] == 2


class TestInputValidation:
    def test_missing_shots_raises(self):
        with pytest.raises(ValueError, match="shots"):
            build_profile_aggregate(
                {
                    "media": _fixture_raws("media")["media"],
                    "shots": None,
                    "story-blocks": None,
                }
            )

    def test_missing_story_raises(self):
        raws = _full_raws()
        raws["story-blocks"] = None
        with pytest.raises(ValueError, match="story-blocks"):
            build_profile_aggregate(raws)

    def test_missing_media_raises(self):
        raws = _full_raws()
        raws["media"] = None
        with pytest.raises(ValueError, match="media"):
            build_profile_aggregate(raws)

    def test_pure_function_does_not_write(self, tmp_path):
        doc = _aggregate()
        assert not (tmp_path / "style-profile.json").exists()
        assert "style-profile.json" not in str(doc)
