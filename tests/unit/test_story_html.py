"""render.story_html 单元测试（roadmap 03-04）。

覆盖：模板替换、五态单元格语义、scaffold/complete 状态、evidence refs 可追溯、
HTML escape、corrections overlay、文档状态推导、缺 story-blocks 报错、
clip 缺失时播放按钮禁用。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memoloupe.core.errors import ArtifactError
from memoloupe.render.story_html import (
    DOCUMENT_TYPE,
    STORY_RENDER_VERSION,
    render_story_html,
)
from memoloupe.validate.html_contract import validate_html

from story_fixtures import asr_doc, media_doc, read_blocks, segment, shots_doc


def _scaffold_blocks_doc() -> dict:
    return {
        "status": "scaffold",
        "boundarySource": "asr-gap",
        "gapMs": 1200,
        "generatedAt": "2026-08-25T00:00:00Z",
        "blocks": [
            {
                "storyBlockID": "B0001",
                "shotIDs": ["SH0001", "SH0002"],
                "startMs": 0,
                "endMs": 6000,
                "boundary": {
                    "level": "start",
                    "signal": "sourceStart",
                    "label": "片头（首镜头强制开块）",
                },
                "divisionAxis": "unknown",
                "divisionRationale": "",
                "primaryRole": "unknown",
                "coreContent": "",
                "informationRole": "unknown",
                "narrativeDensity": "unknown",
                "audienceReaction": "unknown",
                "visualIndependence": "unknown",
                "blockRelation": "",
                "relationReason": "",
            }
        ],
        "slots": [],
    }


def _complete_blocks_doc() -> dict:
    doc = _scaffold_blocks_doc()
    doc["status"] = "complete"
    doc["blocks"][0].update(
        {
            "blockTitle": "出发",
            "divisionAxis": "行动/任务",
            "divisionRationale": "同一行动段落。",
            "primaryRole": "hook",
            "coreContent": "建立旅程起点。<script>alert(1)</script>",
            "informationRole": "建立背景",
            "narrativeDensity": "中",
            "audienceReaction": "好奇/想看下去",
            "visualIndependence": "静音也能看懂",
            "blockRelation": "铺垫 → 下一块",
            "relationReason": "先建立场景。",
        }
    )
    doc["slots"] = [
        {
            "slotID": "S001",
            "slotType": "开场引入",
            "slotTitle": "开场",
            "blockIDs": ["B0001"],
            "slotRationale": "全部块构成开场。",
        }
    ]
    return doc


def _write_out(root: Path, story_doc: dict, *, with_clips: bool = False) -> Path:
    raw = root / "raw"
    raw.mkdir(parents=True)
    (raw / "media.json").write_text(
        json.dumps(media_doc(6000), ensure_ascii=False), encoding="utf-8"
    )
    (raw / "shots.json").write_text(
        json.dumps(shots_doc([(0, 3000), (3000, 6000)]), ensure_ascii=False),
        encoding="utf-8",
    )
    (raw / "asr.json").write_text(
        json.dumps(asr_doc([segment(500, 1500), segment(3000, 5000)]), ensure_ascii=False),
        encoding="utf-8",
    )
    (raw / "story-blocks.json").write_text(
        json.dumps(story_doc, ensure_ascii=False), encoding="utf-8"
    )
    if with_clips:
        (root / "clips").mkdir()
        (root / "clips" / "SH0001.mp4").write_bytes(b"clip")
        (root / "clips" / "SH0002.mp4").write_bytes(b"clip")
    return root


def _errors(issues):
    return [i for i in issues if i.severity == "error"]


class TestRenderBasics:
    def test_renders_blocks_and_slots(self, tmp_path):
        work = _write_out(tmp_path / "out", _complete_blocks_doc())
        target = render_story_html(work)
        assert target == work / "story-analysis.html"
        text = target.read_text(encoding="utf-8")
        assert 'data-document-type="storyAnalysis"' in text
        assert 'data-document-status="draft"' in text
        assert 'data-source-revision="a1b2c3d4e5f6"' in text
        assert 'class="story-block" data-story-block-id="B0001"' in text
        assert 'data-shot-ids="SH0001 SH0002"' in text
        assert 'data-start-ms="0"' in text
        assert 'data-end-ms="6000"' in text
        assert 'data-slot-id="S001"' in text
        assert "开场引入" in text
        # 模板占位符全部替换完毕。
        assert "__DOCUMENT_STATUS__" not in text
        assert "<!--STORY_BLOCKS-->" not in text
        assert "<!--STORY_SLOTS-->" not in text

    def test_missing_story_blocks_raises(self, tmp_path):
        work = tmp_path / "out"
        raw = work / "raw"
        raw.mkdir(parents=True)
        with pytest.raises(ArtifactError):
            render_story_html(work)

    def test_scaffold_cells_are_unknown(self, tmp_path):
        work = _write_out(tmp_path / "out", _scaffold_blocks_doc())
        render_story_html(work)
        text = (work / "story-analysis.html").read_text(encoding="utf-8")
        assert '<td data-field="primaryRole" data-block-id="B0001" data-entity-id="B0001" data-value-state="unknown" data-confidence="unknown"' in text
        assert '<td data-field="coreContent" data-block-id="B0001" data-entity-id="B0001" data-value-state="unknown"' in text
        # 未知值可见但明确：scaffold 不伪装成确定结论。
        assert "待确认" in text

    def test_complete_cells_have_value_semantics(self, tmp_path):
        work = _write_out(tmp_path / "out", _complete_blocks_doc())
        render_story_html(work)
        text = (work / "story-analysis.html").read_text(encoding="utf-8")
        assert 'data-field="primaryRole" data-block-id="B0001" data-entity-id="B0001" data-value-state="value" data-confidence="high" data-evidence-refs="raw/story-blocks.json#blocks[0].primaryRole" data-source="textModel" data-verified="false"' in text
        assert 'data-field="blockTitle"' in text
        assert 'data-field="boundaryBasis"' not in text

    def test_evidence_refs_trace_to_story_blocks(self, tmp_path):
        work = _write_out(tmp_path / "out", _complete_blocks_doc())
        render_story_html(work)
        text = (work / "story-analysis.html").read_text(encoding="utf-8")
        for field in ("primaryRole", "coreContent", "blockRelation"):
            assert f"raw/story-blocks.json#blocks[0].{field}" in text
        for field in ("slotType", "slotTitle", "slotRationale"):
            assert f"raw/story-blocks.json#slots[0].{field}" in text
            assert f"raw/story-blocks.json#blocks[0].{field}" not in text

    def test_model_text_is_html_escaped(self, tmp_path):
        work = _write_out(tmp_path / "out", _complete_blocks_doc())
        render_story_html(work)
        text = (work / "story-analysis.html").read_text(encoding="utf-8")
        assert "<script>alert(1)</script>" not in text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text

    def test_render_version_constant(self):
        assert STORY_RENDER_VERSION.startswith("story-render.")


class TestRenderValidation:
    def test_rendered_page_passes_html_validation_loose(self, tmp_path):
        work = _write_out(tmp_path / "out", _complete_blocks_doc())
        render_story_html(work)
        issues = validate_html(work / "story-analysis.html")
        assert _errors(issues) == []

    def test_rendered_page_passes_html_validation_strict(self, tmp_path):
        work = _write_out(tmp_path / "out", _complete_blocks_doc())
        render_story_html(work)
        issues = validate_html(work / "story-analysis.html", root=work, strict=True)
        assert _errors(issues) == []

    def test_rendered_scaffold_page_passes_strict(self, tmp_path):
        work = _write_out(tmp_path / "out", _scaffold_blocks_doc())
        render_story_html(work)
        issues = validate_html(work / "story-analysis.html", root=work, strict=True)
        assert _errors(issues) == []


class TestCorrectionsOverlay:
    def test_correction_applies_and_status_under_review(self, tmp_path):
        work = _write_out(tmp_path / "out", _complete_blocks_doc())
        corr_dir = work / "corrections"
        corr_dir.mkdir()
        (corr_dir / "storyAnalysis.json").write_text(
            json.dumps(
                {
                    "correctionVersion": 1,
                    "documentType": "storyAnalysis",
                    "sourceRevisionID": "a1b2c3d4e5f6",
                    "changes": [
                        {
                            "entityID": "B0001",
                            "field": "primaryRole",
                            "oldValue": "hook",
                            "newValue": "context",
                            "state": "value",
                            "verified": True,
                            "changedAt": "2026-08-25T08:00:00Z",
                            "actor": "human",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        render_story_html(work)
        text = (work / "story-analysis.html").read_text(encoding="utf-8")
        assert 'data-document-status="underReview"' in text
        assert 'data-source="human"' in text
        assert 'data-verified="true"' in text
        assert 'data-original-value="hook"' in text
        # 确定性展示仍保留（修正只影响呈现，不覆盖 raw）。
        assert read_blocks(work)["blocks"][0]["primaryRole"] == "hook"

    def test_revision_mismatch_marks_outdated(self, tmp_path):
        work = _write_out(tmp_path / "out", _complete_blocks_doc())
        corr_dir = work / "corrections"
        corr_dir.mkdir()
        (corr_dir / "storyAnalysis.json").write_text(
            json.dumps(
                {
                    "correctionVersion": 1,
                    "documentType": "storyAnalysis",
                    "sourceRevisionID": "deadbeef",
                    "changes": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        render_story_html(work)
        text = (work / "story-analysis.html").read_text(encoding="utf-8")
        assert 'data-document-status="outdated"' in text


class TestClipsAndCoverage:
    def test_missing_clips_disable_jump_buttons(self, tmp_path):
        work = _write_out(tmp_path / "out", _complete_blocks_doc())
        render_story_html(work)
        text = (work / "story-analysis.html").read_text(encoding="utf-8")
        assert "暂无片段" in text
        assert 'data-clip-src="clips/SH0001.mp4"' not in text

    def test_existing_clips_allow_playback(self, tmp_path):
        work = _write_out(tmp_path / "out", _complete_blocks_doc(), with_clips=True)
        render_story_html(work)
        text = (work / "story-analysis.html").read_text(encoding="utf-8")
        assert 'data-clip-src="clips/SH0001.mp4"' in text
        assert "暂无片段" not in text

    def test_story_page_layers_phase1_shot_evidence(self, tmp_path):
        work = _write_out(tmp_path / "out", _complete_blocks_doc(), with_clips=True)
        evidence_dir = work / "evidence" / "frames"
        evidence_dir.mkdir(parents=True)
        (evidence_dir / "F_SH0001_MAIN.jpg").write_bytes(b"jpg")
        raw = work / "raw"
        (raw / "frame-evidence.json").write_text(
            json.dumps(
                {
                    "status": "complete",
                    "frames": [
                        {
                            "shotID": "SH0001",
                            "frameType": "representative",
                            "fileRef": "evidence/frames/F_SH0001_MAIN.jpg",
                        }
                    ],
                    "failedFrames": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (raw / "unified-media.json").write_text(
            json.dumps(
                {
                    "status": "complete",
                    "batches": [
                        {
                            "response": {
                                "shots": [
                                    {
                                        "shotID": "SH0001",
                                        "visual": {
                                            "subjects": "旅行者走进乐园入口",
                                            "framing": "全景",
                                            "cameraMovement": "跟",
                                        },
                                        "audio": {"bgmStyle": "轻快电子乐"},
                                        "function": {"shotTone": "轻快"},
                                    }
                                ]
                            }
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (raw / "music-flags.json").write_text(
            json.dumps(
                {"status": "complete", "shots": [{"shotID": "SH0001", "state": "music"}]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (raw / "audio-energy.json").write_text(
            json.dumps(
                {
                    "durationMs": 6000,
                    "hasAudio": True,
                    "sampleRate": 48000,
                    "thresholds": {"silent": -60, "low": -40, "medium": -25, "high": -12},
                    "shots": [
                        {
                            "shotID": "SH0001",
                            "label": "中",
                            "frameCount": 10,
                            "minDb": -42.0,
                            "medianDb": -28.0,
                            "maxDb": -14.0,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        render_story_html(work)
        text = (work / "story-analysis.html").read_text(encoding="utf-8")
        assert "故事层叠加在镜头拉片之上" in text
        assert "镜头证据层" in text
        assert 'class="story-shot-card" data-shot-id="SH0001"' in text
        assert 'src="evidence/frames/F_SH0001_MAIN.jpg"' in text
        assert "旅行者走进乐园入口" in text
        assert "有背景音乐" in text
        assert "音量中" in text

    def test_missing_shots_json_degrades_coverage(self, tmp_path):
        work = _write_out(tmp_path / "out", _complete_blocks_doc())
        (work / "raw" / "shots.json").unlink()
        render_story_html(work)
        text = (work / "story-analysis.html").read_text(encoding="utf-8")
        assert 'data-document-type="storyAnalysis"' in text


def test_document_type_constant():
    assert DOCUMENT_TYPE == "storyAnalysis"
