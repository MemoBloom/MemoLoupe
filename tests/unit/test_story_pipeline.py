"""analysis.story_pipeline 单元测试（roadmap 03-02：确定性 Story Scaffold）。

覆盖：首镜头强制开块、gapMs 边界（小于/等于/大于）、ASR 跨镜头、
无 ASR/failed/skipped 降级、单镜头/无对白/连续对白/多段对白、
block 时间与覆盖、摘要禁带内容、同指纹复用、scaffold 字段合法性。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memoloupe.analysis.story_pipeline import (
    STORY_SCAFFOLD_VERSION,
    StoryAnalysisPipeline,
    StoryAnalysisRequest,
    build_shot_summaries,
    compute_speech_runs,
    segment_of,
)
from memoloupe.artifacts.schemas import ArtifactName, validate_artifact

from story_fixtures import (
    asr_doc,
    media_doc,
    read_blocks,
    segment,
    shots_doc,
    write_out_dir,
)


def _run(root: Path, **kwargs):
    return StoryAnalysisPipeline().run(
        StoryAnalysisRequest(output_dir=root, **kwargs)
    )


# ---------------------------------------------------------------------------
# 纯函数：speech runs 与 segment_of
# ---------------------------------------------------------------------------


class TestSpeechRuns:
    def test_gap_below_threshold_same_run(self):
        runs = compute_speech_runs(
            [segment(500, 1500), segment(2699, 4000)], gap_ms=1200
        )
        assert len(runs) == 1

    def test_gap_equal_threshold_splits(self):
        # 间隔恰好等于 gapMs 也算停顿边界（docs/03 §3.2：>= gapMs）。
        runs = compute_speech_runs(
            [segment(500, 1500), segment(2700, 4000)], gap_ms=1200
        )
        assert len(runs) == 2

    def test_gap_above_threshold_splits(self):
        runs = compute_speech_runs(
            [segment(500, 1500), segment(2701, 4000)], gap_ms=1200
        )
        assert len(runs) == 2

    def test_unsorted_segments_are_sorted(self):
        runs = compute_speech_runs(
            [segment(2700, 4000), segment(500, 1500)], gap_ms=1200
        )
        assert len(runs) == 2
        assert runs[0]["startMs"] == 500
        assert runs[1]["startMs"] == 2700

    def test_overlapping_segments_same_run(self):
        runs = compute_speech_runs(
            [segment(500, 2000), segment(1500, 3000)], gap_ms=1200
        )
        assert len(runs) == 1
        assert runs[0]["endMs"] == 3000

    def test_empty_segments_no_runs(self):
        assert compute_speech_runs([], gap_ms=1200) == []


class TestSegmentOf:
    def test_picks_max_overlap_run(self):
        runs = compute_speech_runs(
            [segment(500, 1500), segment(2700, 4000)], gap_ms=1200
        )
        # [0,3000) 与 run1 重叠 1000ms、与 run2 重叠 300ms → run1。
        assert segment_of(runs, 0, 3000) == 0
        # [3000,6000) 只与 run2 重叠 → run2。
        assert segment_of(runs, 3000, 6000) == 1

    def test_zero_overlap_joins_latest_preceding_run(self):
        # 两段对白之间的无对白镜头归入上一段（尾部静默不独立成块）。
        runs = compute_speech_runs(
            [segment(500, 1500), segment(4500, 5500)], gap_ms=1200
        )
        assert segment_of(runs, 2000, 4000) == 0

    def test_leading_shot_joins_first_run(self):
        # 首段对白开始前的镜头没有"上一段" → 归入最早的 run。
        runs = compute_speech_runs([segment(5000, 6000)], gap_ms=1200)
        assert segment_of(runs, 0, 1000) == 0

    def test_no_runs_returns_sentinel(self):
        assert segment_of([], 0, 1000) == -1


# ---------------------------------------------------------------------------
# 摘要：禁带内容
# ---------------------------------------------------------------------------


class TestShotSummaries:
    def test_summary_contains_text_fields(self):
        unified = {
            "service": "unifiedAudioVideo",
            "status": "complete",
            "batches": [
                {
                    "response": {
                        "shots": [
                            {
                                "shotID": "SH0001",
                                "visual": {
                                    "subjects": "旅客",
                                    "actions": "拖行李走动",
                                    "setting": "机场",
                                    "props": "行李箱",
                                },
                                "components": {
                                    "texts": [{"textContent": "DAY 1"}]
                                },
                            }
                        ]
                    }
                }
            ],
        }
        raws = {
            "shots": shots_doc([(0, 3203)]),
            "asr": asr_doc([segment(820, 2460, "今天我们从机场出发。")]),
            "unified-media": unified,
        }
        summaries = build_shot_summaries(raws)
        assert len(summaries) == 1
        s = summaries[0]
        assert s["shotID"] == "SH0001"
        assert s["startMs"] == 0 and s["endMs"] == 3203
        assert s["visual"]["contentSummary"] == "旅客；拖行李走动；机场；行李箱"
        assert s["visual"]["subjects"] == "旅客"
        assert s["visual"]["actions"] == "拖行李走动"
        assert s["visual"]["setting"] == "机场"
        assert s["speech"] == "今天我们从机场出发。"
        assert s["texts"] == ["DAY 1"]
        assert s["editing"]["transition"] == "硬切"

    def test_summary_forbids_binary_and_paths(self):
        # 摘要不得携带 clip/帧 Data URI/源视频路径/模型代理路径（03-02 铁律）。
        unified = {
            "service": "unifiedAudioVideo",
            "status": "complete",
            "clips": [
                {"shotID": "SH0001", "clipPath": "clips/SH0001.mp4",
                 "proxyPath": "proxy/SH0001.mp4"}
            ],
            "batches": [
                {
                    "response": {
                        "shots": [
                            {
                                "shotID": "SH0001",
                                "visual": {
                                    "subjects": "旅客",
                                    "actions": "行走",
                                    "setting": "机场",
                                    "props": "行李箱",
                                },
                                "components": {"texts": []},
                                "evidenceRefs": [
                                    "raw/shots.json#shots[0]",
                                    "evidence/frames/F_SH0001_MAIN.jpg",
                                ],
                            }
                        ]
                    }
                }
            ],
        }
        raws = {
            "shots": shots_doc([(0, 3203)]),
            "asr": asr_doc([segment(820, 2460)]),
            "unified-media": unified,
        }
        payload = json.dumps(build_shot_summaries(raws), ensure_ascii=False)
        assert "data:" not in payload
        assert "clips/" not in payload
        assert ".mp4" not in payload
        assert "evidence/frames/" not in payload
        assert "/tmp/story-test" not in payload

    def test_summary_tolerates_missing_unified_and_asr(self):
        raws = {"shots": shots_doc([(0, 3203)]), "asr": None, "unified-media": None}
        summaries = build_shot_summaries(raws)
        assert summaries[0]["shotID"] == "SH0001"
        assert summaries[0]["speech"] == ""


# ---------------------------------------------------------------------------
# 管线：聚块行为
# ---------------------------------------------------------------------------


class TestScaffoldBlocking:
    def test_single_shot_single_block(self, tmp_path):
        work = write_out_dir(
            tmp_path / "out", shot_ranges=[(0, 3203)],
            asr=asr_doc([segment(820, 2460)]),
        )
        report = _run(work)
        assert report.status == "complete"
        doc = read_blocks(work)
        assert [b["shotIDs"] for b in doc["blocks"]] == [["SH0001"]]

    def test_first_shot_always_opens_block(self, tmp_path):
        # 所有镜头同属一个 run 时也必须因 sentinel 开出首块。
        work = write_out_dir(
            tmp_path / "out", shot_ranges=[(0, 3000), (3000, 6000)],
            asr=asr_doc([segment(500, 5500)]),
        )
        _run(work)
        doc = read_blocks(work)
        assert len(doc["blocks"]) == 1
        assert doc["blocks"][0]["storyBlockID"] == "B0001"

    def test_segment_spanning_shots_keeps_single_block(self, tmp_path):
        # 一条 ASR segment 跨两个镜头 → 同一 run → 同一块。
        work = write_out_dir(
            tmp_path / "out", shot_ranges=[(0, 3000), (3000, 6000)],
            asr=asr_doc([segment(1000, 5000)]),
        )
        _run(work)
        assert len(read_blocks(work)["blocks"]) == 1

    def test_gap_boundary_below_equal_above(self, tmp_path):
        # 显式 gap_ms=1200 锁定 >= 边界语义（小于/等于/大于），与默认值无关。
        for gap, expected_blocks in ((1199, 1), (1200, 2), (1201, 2)):
            work = write_out_dir(
                tmp_path / f"out{gap}", shot_ranges=[(0, 3000), (3000, 6000)],
                asr=asr_doc(
                    [segment(500, 1500), segment(1500 + gap, 5000)]
                ),
            )
            _run(work, gap_ms=1200)
            assert len(read_blocks(work)["blocks"]) == expected_blocks, f"gap={gap}"

    def test_silent_shot_between_runs_stays_in_previous_block(self, tmp_path):
        work = write_out_dir(
            tmp_path / "out",
            shot_ranges=[(0, 2000), (2000, 4000), (4000, 6000)],
            asr=asr_doc([segment(500, 1500), segment(4500, 5500)]),
        )
        _run(work)
        blocks = read_blocks(work)["blocks"]
        assert [b["shotIDs"] for b in blocks] == [
            ["SH0001", "SH0002"],
            ["SH0003"],
        ]

    def test_continuous_speech_single_block(self, tmp_path):
        work = write_out_dir(
            tmp_path / "out",
            shot_ranges=[(0, 2000), (2000, 4000), (4000, 6000)],
            asr=asr_doc(
                [segment(200, 1800), segment(2100, 3900), segment(4200, 5800)]
            ),
        )
        _run(work)
        assert len(read_blocks(work)["blocks"]) == 1

    def test_multiple_speech_runs_multiple_blocks(self, tmp_path):
        work = write_out_dir(
            tmp_path / "out",
            shot_ranges=[(0, 2000), (2000, 4000), (4000, 6000)],
            asr=asr_doc([segment(200, 1800), segment(4200, 5800)]),
        )
        _run(work)
        blocks = read_blocks(work)["blocks"]
        assert len(blocks) == 2
        assert blocks[0]["storyBlockID"] == "B0001"
        assert blocks[1]["storyBlockID"] == "B0002"

    def test_block_times_from_shot_final_boundaries(self, tmp_path):
        work = write_out_dir(
            tmp_path / "out",
            shot_ranges=[(0, 2000), (2000, 4000), (4000, 6000)],
            asr=asr_doc([segment(200, 1800), segment(4200, 5800)]),
        )
        _run(work)
        blocks = read_blocks(work)["blocks"]
        assert (blocks[0]["startMs"], blocks[0]["endMs"]) == (0, 4000)
        assert (blocks[1]["startMs"], blocks[1]["endMs"]) == (4000, 6000)

    def test_blocks_cover_all_shots_in_order(self, tmp_path):
        work = write_out_dir(
            tmp_path / "out",
            shot_ranges=[(0, 1500), (1500, 3000), (3000, 4500), (4500, 6000)],
            asr=asr_doc([segment(100, 1200), segment(3200, 4400)]),
        )
        _run(work)
        blocks = read_blocks(work)["blocks"]
        covered = [sid for b in blocks for sid in b["shotIDs"]]
        assert covered == ["SH0001", "SH0002", "SH0003", "SH0004"]


class TestNoAsrDegradation:
    @pytest.mark.parametrize("asr", [
        None,  # asr.json 缺失
        asr_doc([], status="skipped"),
        asr_doc([], status="failed"),
        asr_doc([]),  # complete 但没有任何 segment
    ])
    def test_conservative_single_block(self, tmp_path, asr):
        work = write_out_dir(
            tmp_path / "out",
            shot_ranges=[(0, 2000), (2000, 4000), (4000, 6000)],
            asr=asr,
        )
        report = _run(work)
        assert report.status == "complete"
        doc = read_blocks(work)
        assert doc["status"] == "scaffold"
        assert len(doc["blocks"]) == 1
        block = doc["blocks"][0]
        assert block["shotIDs"] == ["SH0001", "SH0002", "SH0003"]
        assert block["boundary"]["signal"] == "none"


class TestScaffoldDocument:
    def test_scaffold_fields_and_schema(self, tmp_path):
        work = write_out_dir(
            tmp_path / "out", shot_ranges=[(0, 3000), (3000, 6000)],
            asr=asr_doc([segment(500, 1500), segment(3000, 5000)]),
        )
        # 显式 gap_ms=1200：本用例锁定的是两块场景的字段/信号，与默认值无关。
        _run(work, gap_ms=1200)
        doc = read_blocks(work)
        # 写盘已过 schema；再显式校验一次锁定契约。
        validate_artifact(ArtifactName.STORY_BLOCKS, doc)
        assert doc["status"] == "scaffold"
        assert doc["boundarySource"] == "asr-gap"
        assert doc["gapMs"] == 1200
        assert doc["slots"] == []
        for block in doc["blocks"]:
            # 叙事字段：枚举落 unknown，自由文本落空，不伪造语义。
            assert block["divisionAxis"] == "unknown"
            assert block["primaryRole"] == "unknown"
            assert block["informationRole"] == "unknown"
            assert block["narrativeDensity"] == "unknown"
            assert block["audienceReaction"] == "unknown"
            assert block["visualIndependence"] == "unknown"
            assert block["coreContent"] == ""
            assert block["blockRelation"] == ""
        # 首块信号 sourceStart，后续块 asr-gap。
        assert doc["blocks"][0]["boundary"]["signal"] == "sourceStart"
        assert doc["blocks"][1]["boundary"]["signal"] == "asr-gap"

    def test_custom_gap_ms_recorded(self, tmp_path):
        work = write_out_dir(
            tmp_path / "out", shot_ranges=[(0, 3000), (3000, 6000)],
            asr=asr_doc([segment(500, 1500), segment(3000, 5000)]),
        )
        _run(work, gap_ms=2000)
        assert read_blocks(work)["gapMs"] == 2000


class TestCheckpointReuse:
    def test_same_fingerprint_reused_on_second_run(self, tmp_path):
        work = write_out_dir(
            tmp_path / "out", shot_ranges=[(0, 3000), (3000, 6000)],
            asr=asr_doc([segment(500, 1500), segment(3000, 5000)]),
        )
        first = _run(work)
        first_doc = read_blocks(work)
        second = _run(work)
        second_doc = read_blocks(work)
        step = next(s for s in second.steps if s.name == "scaffold_story_blocks")
        assert step.status == "reused"
        # 复用不重生成：generatedAt 不变。
        assert second_doc["generatedAt"] == first_doc["generatedAt"]
        assert first.status == "complete" and second.status == "complete"

    def test_gap_change_invalidates_reuse(self, tmp_path):
        work = write_out_dir(
            tmp_path / "out", shot_ranges=[(0, 3000), (3000, 6000)],
            asr=asr_doc([segment(500, 1500), segment(2800, 5000)]),
        )
        _run(work, gap_ms=1200)
        assert len(read_blocks(work)["blocks"]) == 2
        _run(work, gap_ms=2000)
        assert len(read_blocks(work)["blocks"]) == 1

    def test_force_reruns_step(self, tmp_path):
        work = write_out_dir(
            tmp_path / "out", shot_ranges=[(0, 3000)],
            asr=asr_doc([segment(500, 1500)]),
        )
        _run(work)
        report = _run(work, force=frozenset({"scaffold_story_blocks"}))
        step = next(s for s in report.steps if s.name == "scaffold_story_blocks")
        assert step.status == "complete"


class TestInputValidation:
    def test_missing_shots_fails(self, tmp_path):
        raw = tmp_path / "out" / "raw"
        raw.mkdir(parents=True)
        (raw / "media.json").write_text(
            json.dumps(media_doc(), ensure_ascii=False), encoding="utf-8"
        )
        report = _run(tmp_path / "out")
        assert report.status == "failed"
        assert not (tmp_path / "out" / "raw" / "story-blocks.json").exists()


def test_scaffold_version_constant():
    assert STORY_SCAFFOLD_VERSION.startswith("story-scaffold.")
