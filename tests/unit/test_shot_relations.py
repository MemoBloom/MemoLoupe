"""analysis.shot_relations 单元测试：pair 枚举、确定性指标、语义合并红线。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memoloupe.analysis.shot_relation_prompts import (
    PairSemanticParseError,
    parse_pair_semantics,
)
from memoloupe.analysis.shot_relations import (
    build_shot_relations,
    build_shot_relations_stub,
    _speech_metrics,
)
from memoloupe.services.mock import MockShotRelationService


def _shots(n: int = 3) -> list[dict]:
    shots = []
    ms = 0
    for i in range(n):
        shots.append(
            {
                "shotID": f"SH{i + 1:04d}",
                "finalStartMs": ms,
                "finalEndMs": ms + 1000,
                "durationMs": 1000,
                "boundaryIn": {"type": "hardCutCandidate"},
                "boundaryOut": {"type": "hardCutCandidate"},
            }
        )
        ms += 1000
    return shots


def _build(tmp_path: Path, shots, **overrides) -> dict:
    from memoloupe.media.proc import ProcessError, ProcessResult

    class _FailingPool:
        """每个 ffmpeg 调用都失败（模拟无源/无工具），边界帧显式 failed。"""

        def run(self, argv, *, timeout_sec, stdin=None, capture_limit_bytes=None):
            raise ProcessError(
                ProcessResult(argv=tuple(str(a) for a in argv), returncode=1,
                              stdout=b"", stderr=b"fixture", elapsed_sec=0.001)
            )

    defaults: dict = {
        "pool": _FailingPool(),
        "model_service": None,
        "source_revision_id": "a1b2c3d4e5f6",
        "energy_doc": None,
        "music_doc": None,
        "camera_doc": None,
        "asr_doc": None,
        "audio_cuts_doc": None,
    }
    defaults.update(overrides)
    config = {"ffmpeg": {"ffmpegPath": "ffmpeg", "frameTimeoutSec": 5.0}}
    # 边界帧提取依赖 ffmpeg；用失败 pool 让各帧显式 failed，
    # 从而聚焦 pair 枚举与指标/语义合并逻辑。
    return build_shot_relations(
        Path("missing-source.mp4"), shots, config, tmp_path, **defaults
    )


class TestPairEnumeration:
    def test_strict_n_minus_1_pairs_in_order(self, tmp_path) -> None:
        shots = _shots(4)
        doc = _build(tmp_path, shots)
        assert doc["analysis"]["pairCount"] == 3
        assert [r["pairID"] for r in doc["relations"]] == [
            "SH0001--SH0002",
            "SH0002--SH0003",
            "SH0003--SH0004",
        ]

    def test_single_shot_yields_empty_relations(self, tmp_path) -> None:
        doc = _build(tmp_path, _shots(1))
        assert doc["relations"] == []
        assert doc["status"] == "complete"

    def test_boundary_equals_left_final_end(self, tmp_path) -> None:
        doc = _build(tmp_path, _shots(3))
        for i, rel in enumerate(doc["relations"]):
            assert rel["boundaryMs"] == shots_final_end(i)


def shots_final_end(i: int) -> int:
    return (i + 1) * 1000


class TestDeterministicMetrics:
    def test_missing_inputs_land_unknown_not_absent(self, tmp_path) -> None:
        doc = _build(tmp_path, _shots(2))
        metrics = doc["relations"][0]["metrics"]
        assert metrics["audioLevelDeltaDb"]["status"] == "unknown"
        assert metrics["cameraMotionChange"]["status"] == "unknown"
        assert metrics["speechGapMs"]["status"] == "unknown"
        assert metrics["musicContinuity"]["status"] == "unknown"
        # 边界帧不可用 → lumaDelta 显式 unavailable + 复核提示
        assert metrics["lumaDelta"]["status"] == "unavailable"
        assert doc["relations"][0]["review"]["needsReview"] is True

    def test_energy_delta_from_artifacts(self, tmp_path) -> None:
        energy = {
            "shots": [
                {"shotID": "SH0001", "medianDb": -20.0},
                {"shotID": "SH0002", "medianDb": -15.0},
            ]
        }
        doc = _build(tmp_path, _shots(2), energy_doc=energy)
        metric = doc["relations"][0]["metrics"]["audioLevelDeltaDb"]
        assert metric["status"] == "value"
        assert metric["value"] == pytest.approx(5.0)
        assert metric["evidenceRefs"] == [
            "raw/audio-energy.json#shots[0]",
            "raw/audio-energy.json#shots[1]",
        ]

    def test_audio_cut_alignment(self, tmp_path) -> None:
        cuts = {"boundaries": [{"timeMs": 1005}]}
        doc = _build(tmp_path, _shots(2), audio_cuts_doc=cuts)
        metric = doc["relations"][0]["metrics"]["audioCutAligned"]
        assert metric["value"] is True

    def test_speech_gap_and_span(self) -> None:
        segments = [
            {"startMs": 0, "endMs": 900},
            {"startMs": 2000, "endMs": 3000},
        ]
        gap, spans, refs = _speech_metrics(segments, boundary_ms=1000)
        assert gap["value"] == 1100
        assert spans["value"] is False
        assert refs == [
            "raw/asr.json#transcript.segments[0]",
            "raw/asr.json#transcript.segments[1]",
        ]

        crossing = [{"startMs": 500, "endMs": 1500}]
        gap2, spans2, _ = _speech_metrics(crossing, boundary_ms=1000)
        assert gap2["value"] == 0
        assert spans2["value"] is True


class TestSemanticLayer:
    def test_no_service_semantic_unknown(self, tmp_path) -> None:
        doc = _build(tmp_path, _shots(2), model_service=None)
        semantic = doc["relations"][0]["semantic"]
        assert semantic["status"] == "unknown"
        assert "未配置" in semantic["reason"]

    def test_mock_service_produces_complete_semantic(self, tmp_path) -> None:
        doc = _build(tmp_path, _shots(2), model_service=MockShotRelationService())
        semantic = doc["relations"][0]["semantic"]
        assert semantic["status"] == "complete"
        assert semantic["fields"]["actionContinuity"]["value"] == "无法判断"

    def test_invalid_json_does_not_pollute_metrics(self, tmp_path) -> None:
        class BadService:
            def marker(self) -> str:
                return "bad"

            def analyze_pair(self, payload: dict) -> str:
                return "not-json{{"

        doc = _build(tmp_path, _shots(2), model_service=BadService())
        assert doc["status"] == "partial"
        assert doc["relations"][0]["semantic"]["status"] == "failed"
        # 确定性指标不受模型失败影响
        assert "audioLevelDeltaDb" in doc["relations"][0]["metrics"]

    def test_service_exception_is_explicit_failure(self, tmp_path) -> None:
        class ExplodingService:
            def marker(self) -> str:
                return "explode"

            def analyze_pair(self, payload: dict) -> str:
                raise RuntimeError("network down")

        doc = _build(tmp_path, _shots(2), model_service=ExplodingService())
        semantic = doc["relations"][0]["semantic"]
        assert semantic["status"] == "failed"
        assert "语义服务失败" in semantic["reason"]


class TestParsePairSemantics:
    REF = "raw/shot-relations.json#relations[0]"

    def test_valid_response(self) -> None:
        text = json.dumps(
            {
                "actionContinuity": "动作承接",
                "eyelineContinuity": "不适用",
                "screenDirection": "方向连续",
                "spatialTemporalRelation": "同空间连续",
                "editMotivations": ["动作", "对白"],
                "relationSummary": "动作跨切衔接。",
            },
            ensure_ascii=False,
        )
        parsed = parse_pair_semantics(text, ref_base=self.REF)
        fields = parsed["fields"]
        assert fields["actionContinuity"]["state"] == "value"
        assert fields["actionContinuity"]["source"] == "textModel"
        assert fields["editMotivations"]["value"] == ["动作", "对白"]
        # 五态红线：模型语义的 absent-claimed 与 absent 必须不同
        assert fields["editMotivations"]["state"] == "value"

    def test_enum_violation_lands_unknown(self) -> None:
        text = json.dumps(
            {
                "actionContinuity": "超级无敌衔接",  # 越界
                "eyelineContinuity": "不适用",
                "screenDirection": "方向连续",
                "spatialTemporalRelation": "同空间连续",
                "editMotivations": ["不存在的动机"],
                "relationSummary": "ok",
            },
            ensure_ascii=False,
        )
        parsed = parse_pair_semantics(text, ref_base=self.REF)
        assert parsed["fields"]["actionContinuity"]["state"] == "unknown"
        assert "越界" in parsed["fields"]["actionContinuity"]["note"]
        # 其他字段不受影响
        assert parsed["fields"]["screenDirection"]["state"] == "value"
        assert parsed["fields"]["editMotivations"]["state"] == "absent-claimed"
        assert parsed["issues"]

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(PairSemanticParseError):
            parse_pair_semantics("```json\n{broken", ref_base=self.REF)

    def test_missing_fields_land_unknown(self) -> None:
        parsed = parse_pair_semantics("{}", ref_base=self.REF)
        for name in ("actionContinuity", "relationSummary"):
            assert parsed["fields"][name]["state"] == "unknown"


class TestStub:
    def test_stub_keeps_pair_set(self) -> None:
        doc = build_shot_relations_stub(_shots(3), {}, "rev", "--skip")
        assert doc["status"] == "failed"
        assert [r["pairID"] for r in doc["relations"]] == [
            "SH0001--SH0002",
            "SH0002--SH0003",
        ]
        assert doc["relations"][0]["metrics"] == {}


class TestBoundaryFrameTimes:
    """PTS 索引定位边界帧：避免 endMs-1 与 startMs 落入同一展示帧。"""

    def test_uses_real_pts(self) -> None:
        from memoloupe.media.transition_evidence import (
            boundary_frame_times_from_pts,
        )

        pts = [0, 33, 66, 100, 133]  # 30fps 实际 PTS
        left_t, right_t = boundary_frame_times_from_pts(
            pts, boundary_ms=100, left_end_ms=100, right_start_ms=100
        )
        assert left_t == 66  # 严格小于切点的最后一帧
        assert right_t == 100  # 大于等于切点的第一帧

    def test_falls_back_without_index(self) -> None:
        from memoloupe.media.transition_evidence import (
            boundary_frame_times_from_pts,
        )

        left_t, right_t = boundary_frame_times_from_pts(
            None, boundary_ms=100, left_end_ms=100, right_start_ms=100
        )
        assert left_t == 99
        assert right_t == 100
