"""analysis.asr_stage 单元测试（docs/03 §2.7）。

覆盖：skipped/failed/complete 三态、segments 排序与 clamp 校验、
shot_speech 归属规则（交集比例 ≥0.5）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memoloupe.analysis.asr_stage import (
    ASR_STAGE_VERSION,
    run_asr_stage,
    shot_speech,
    shot_speech_segments,
)
from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
from memoloupe.core.errors import CapabilityUnavailableError
from memoloupe.services.asr import ASRResult
from memoloupe.services.mock import MockASRService


def _media(audio: bool = True, start_ms: int = 0, end_ms: int = 4000) -> dict:
    return {
        "source": {
            "durationMs": end_ms,
            "analyzedRange": {"startMs": start_ms, "endMs": end_ms},
            "audioTracks": [{"codec": "aac"}] if audio else [],
        }
    }


def _segments(*specs: tuple[int, int, str]) -> list[dict]:
    return [{"startMs": s, "endMs": e, "text": t} for s, e, t in specs]


class TestRunAsrStage:
    def test_service_none_is_skipped_stub(self, tmp_path):
        doc = run_asr_stage(tmp_path / "a.mp4", _media(), {}, service=None)
        validate_artifact(ArtifactName.ASR, doc)
        assert doc["service"] == "asr"
        assert doc["status"] == "skipped"
        assert doc["transcript"]["segments"] == []
        assert doc["note"]

    def test_capability_unavailable_is_skipped(self, tmp_path):
        service = MockASRService(error=CapabilityUnavailableError("asr", "未配置 api_key"))
        doc = run_asr_stage(tmp_path / "a.mp4", _media(), {}, service=service)
        validate_artifact(ArtifactName.ASR, doc)
        assert doc["status"] == "skipped"
        assert "api_key" in doc["note"]

    def test_no_audio_track_is_skipped_without_calling_service(self, tmp_path):
        service = MockASRService(segments=_segments((0, 1000, "你好")))
        doc = run_asr_stage(tmp_path / "a.mp4", _media(audio=False), {}, service=service)
        assert doc["status"] == "skipped"
        assert service.calls == []  # 无音轨不应发起请求

    def test_success_writes_complete_transcript(self, tmp_path):
        service = MockASRService(
            segments=_segments((100, 1500, "第一段。"), (2000, 3000, "第二段。"))
        )
        source = tmp_path / "a.mp4"
        source.write_bytes(b"fake")
        doc = run_asr_stage(source, _media(), {}, service=service)
        validate_artifact(ArtifactName.ASR, doc)
        assert doc["status"] == "complete"
        assert doc["transcript"]["text"] == "第一段。 第二段。"
        assert [s["text"] for s in doc["transcript"]["segments"]] == ["第一段。", "第二段。"]
        # 整片调用一次，时间窗对齐 analyzedRange
        assert len(service.calls) == 1
        _, request = service.calls[0]
        assert (request.start_ms, request.end_ms) == (0, 4000)

    def test_unsorted_segments_are_sorted_with_warning(self, tmp_path):
        service = MockASRService(
            segments=_segments((2000, 3000, "后"), (100, 1500, "前"))
        )
        doc = run_asr_stage(tmp_path / "a.mp4", _media(), {}, service=service)
        assert doc["status"] == "complete"
        assert [s["text"] for s in doc["transcript"]["segments"]] == ["前", "后"]
        assert any("升序" in w for w in doc["warnings"])

    def test_out_of_range_segments_are_clamped_with_warning(self, tmp_path):
        service = MockASRService(segments=_segments((0, 5000, "越界句")))
        doc = run_asr_stage(tmp_path / "a.mp4", _media(end_ms=4000), {}, service=service)
        seg = doc["transcript"]["segments"][0]
        assert (seg["startMs"], seg["endMs"]) == (0, 4000)
        assert any("clamp" in w for w in doc["warnings"])

    def test_segment_fully_outside_range_is_dropped(self, tmp_path):
        service = MockASRService(segments=_segments((5000, 6000, "片外"), (0, 500, "片内")))
        doc = run_asr_stage(tmp_path / "a.mp4", _media(end_ms=4000), {}, service=service)
        assert [s["text"] for s in doc["transcript"]["segments"]] == ["片内"]
        assert any("analyzedRange" in w for w in doc["warnings"])

    def test_degenerate_segment_is_dropped(self, tmp_path):
        service = MockASRService(segments=_segments((1000, 1000, "零长"), (0, 500, "正常")))
        doc = run_asr_stage(tmp_path / "a.mp4", _media(), {}, service=service)
        assert [s["text"] for s in doc["transcript"]["segments"]] == ["正常"]
        assert doc["warnings"]

    def test_service_exception_is_failed_with_diagnostics(self, tmp_path):
        service = MockASRService(error=RuntimeError("connection reset"))
        doc = run_asr_stage(tmp_path / "a.mp4", _media(), {}, service=service)
        validate_artifact(ArtifactName.ASR, doc)
        assert doc["status"] == "failed"
        assert doc["error"]["type"] == "RuntimeError"
        assert "connection reset" in doc["error"]["message"]

    def test_stage_version_constant(self):
        assert ASR_STAGE_VERSION == "asr.v1"


class TestShotSpeech:
    def test_positive_overlap_joins_in_order(self):
        segments = _segments((100, 900, "一"), (1000, 1800, "二"))
        assert shot_speech(segments, 0, 2000) == "一 二"

    def test_no_overlap_returns_none(self):
        segments = _segments((3000, 3500, "远处"))
        assert shot_speech(segments, 0, 2000) is None

    def test_boundary_segment_majority_inside_is_attributed(self):
        # segment [1500, 2500) 与镜头 [0, 2000) 交集 500/1000 = 0.5 → 归属
        segments = _segments((1500, 2500, "跨镜句"))
        assert shot_speech(segments, 0, 2000) == "跨镜句"

    def test_boundary_segment_minority_inside_is_not_attributed(self):
        # 交集 400/1000 < 0.5 → 不归属
        segments = _segments((1600, 2600, "跨镜句"))
        assert shot_speech(segments, 0, 2000) is None

    def test_straddling_sentence_can_belong_to_both_shots(self):
        # 50/50 跨界句：两侧交集比例都 >= 0.5，两侧都引用
        segments = _segments((1500, 2500, "跨镜句"))
        assert shot_speech(segments, 0, 2000) == "跨镜句"
        assert shot_speech(segments, 2000, 4000) == "跨镜句"

    def test_empty_text_segment_is_ignored(self):
        segments = _segments((100, 900, "   "))
        assert shot_speech(segments, 0, 2000) is None

    def test_segments_helper_returns_original_indexes(self):
        segments = _segments((100, 900, "一"), (3000, 3500, "外"), (1000, 1800, "二"))
        hits = shot_speech_segments(segments, 0, 2000)
        assert [i for i, _ in hits] == [0, 2]
        assert [s["text"] for _, s in hits] == ["一", "二"]
