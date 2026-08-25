"""services/mock 单元测试：可编程 mock 的全部编排形态。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memoloupe.services.asr import ASRRequest, ASRResult, ASRService
from memoloupe.services.base import PermanentServiceError, TransientServiceError
from memoloupe.services.mock import (
    GROUP_OWNED_SECTIONS,
    MockASRService,
    MockUnifiedMediaService,
    default_mock_unified,
)
from memoloupe.services.unified_media import (
    AnalysisGroup,
    ModelClip,
    UnifiedMediaService,
)


def _clips(*shot_ids: str) -> list[ModelClip]:
    return [
        ModelClip(shot_id=s, proxy_path=Path(f"clips/model-proxy/{s}.mp4"), duration_ms=1000)
        for s in shot_ids
    ]


def _group(name: str) -> AnalysisGroup:
    return AnalysisGroup(
        name=name, fields=(), prompt="p", schema={}, fingerprint=f"fp-{name}"
    )


class TestMockASRService:
    def test_returns_segments(self):
        svc = MockASRService(segments=[{"startMs": 0, "endMs": 100, "text": "嗨"}])
        result = svc.transcribe(Path("a.mp4"), ASRRequest())
        assert isinstance(result, ASRResult)
        assert result.segments[0]["text"] == "嗨"
        assert isinstance(result.segments, tuple)

    def test_raises_programmed_error(self):
        err = TransientServiceError("timeout")
        svc = MockASRService(error=err)
        with pytest.raises(TransientServiceError):
            svc.transcribe(Path("a.mp4"), ASRRequest())

    def test_records_calls(self):
        svc = MockASRService()
        svc.transcribe(Path("a.mp4"), ASRRequest(language="zh"))
        assert len(svc.calls) == 1
        assert svc.calls[0][1].language == "zh"

    def test_satisfies_protocol(self):
        assert isinstance(MockASRService(), ASRService)


class TestMockUnifiedMediaService:
    def test_dict_script_by_group_and_shots(self):
        text = '{"shots": []}'
        svc = MockUnifiedMediaService({("visual", ("SH0001", "SH0002")): text})
        assert svc.analyze_batch(_clips("SH0001", "SH0002"), _group("visual")) == text

    def test_dict_script_by_call_index(self):
        svc = MockUnifiedMediaService({0: "first", 1: "second"})
        assert svc.analyze_batch(_clips("SH0001"), _group("visual")) == "first"
        assert svc.analyze_batch(_clips("SH0002"), _group("audio")) == "second"

    def test_callable_script(self):
        def script(clips, group, call_index):
            return f"{group.name}:{','.join(c.shot_id for c in clips)}:{call_index}"

        svc = MockUnifiedMediaService(script)
        assert svc.analyze_batch(_clips("SH0001"), _group("audio")) == "audio:SH0001:0"
        assert svc.analyze_batch(_clips("SH0002"), _group("visual")) == "visual:SH0002:1"

    def test_fence_and_invalid_json_returned_verbatim(self):
        fenced = '```json\n{"shots": []}\n```'
        svc = MockUnifiedMediaService(
            {("visual", ("SH0001",)): fenced, ("audio", ("SH0001",)): "not json {"}
        )
        assert svc.analyze_batch(_clips("SH0001"), _group("visual")) == fenced
        assert svc.analyze_batch(_clips("SH0001"), _group("audio")) == "not json {"

    def test_missing_duplicate_unknown_shot_payloads(self):
        # 端口层原样透传；对齐校验是编排器职责
        missing = json.dumps({"shots": [{"shotID": "SH0001", "visual": {}}]})
        dup = json.dumps(
            {
                "shots": [
                    {"shotID": "SH0001", "visual": {}},
                    {"shotID": "SH0001", "visual": {}},
                ]
            }
        )
        unknown = json.dumps({"shots": [{"shotID": "SH9999", "visual": {}}]})
        svc = MockUnifiedMediaService({0: missing, 1: dup, 2: unknown})
        assert json.loads(svc.analyze_batch(_clips("SH0001", "SH0002"), _group("visual")))["shots"][0]["shotID"] == "SH0001"
        assert len(json.loads(svc.analyze_batch(_clips("SH0001"), _group("visual")))["shots"]) == 2
        assert json.loads(svc.analyze_batch(_clips("SH0001"), _group("visual")))["shots"][0]["shotID"] == "SH9999"

    def test_wu_value_passthrough(self):
        text = json.dumps({"shots": [{"shotID": "SH0001", "audio": {"speech": "无"}}]})
        svc = MockUnifiedMediaService({0: text})
        result = json.loads(svc.analyze_batch(_clips("SH0001"), _group("audio")))
        assert result["shots"][0]["audio"]["speech"] == "无"

    def test_exception_outcomes(self):
        svc = MockUnifiedMediaService(
            {
                0: TransientServiceError("HTTP 429"),
                1: TransientServiceError("HTTP 500"),
                2: TransientServiceError("timeout"),
                3: PermanentServiceError("HTTP 401"),
            }
        )
        for _ in range(3):
            with pytest.raises(TransientServiceError):
                svc.analyze_batch(_clips("SH0001"), _group("visual"))
        with pytest.raises(PermanentServiceError):
            svc.analyze_batch(_clips("SH0001"), _group("visual"))

    def test_unscripted_call_raises(self):
        svc = MockUnifiedMediaService({})
        with pytest.raises(KeyError):
            svc.analyze_batch(_clips("SH0001"), _group("visual"))

    def test_records_calls(self):
        svc = MockUnifiedMediaService({0: "{}", 1: "{}"})
        svc.analyze_batch(_clips("SH0001", "SH0002"), _group("visual"))
        svc.analyze_batch(_clips("SH0003"), _group("audio"))
        assert svc.calls[0]["group"] == "visual"
        assert svc.calls[0]["shot_ids"] == ("SH0001", "SH0002")
        assert svc.calls[1]["group"] == "audio"
        assert svc.calls[1]["shot_ids"] == ("SH0003",)

    def test_satisfies_protocol(self):
        assert isinstance(MockUnifiedMediaService({}), UnifiedMediaService)


class TestDefaultMockUnified:
    def test_all_groups_return_valid_json(self):
        svc = default_mock_unified(["SH0001", "SH0002"])
        for name in ("visual", "audio", "editing_function"):
            text = svc.analyze_batch(_clips("SH0001", "SH0002"), _group(name))
            data = json.loads(text)
            assert [s["shotID"] for s in data["shots"]] == ["SH0001", "SH0002"]

    def test_each_group_only_owns_its_sections(self):
        svc = default_mock_unified(["SH0001"])
        for name, owned in GROUP_OWNED_SECTIONS.items():
            text = svc.analyze_batch(_clips("SH0001"), _group(name))
            shot = json.loads(text)["shots"][0]
            for section in ("visual", "function", "audio", "components", "editing"):
                if section in owned:
                    assert section in shot, f"{name} 应拥有 {section}"
                else:
                    assert section not in shot, f"{name} 不应拥有 {section}"
            assert set(shot["confidence"]) == set(owned.get("confidence", ()))

    def test_visual_group_fields_match_modelshot_schema(self):
        svc = default_mock_unified(["SH0001"])
        shot = json.loads(svc.analyze_batch(_clips("SH0001"), _group("visual")))["shots"][0]
        for field in (
            "content", "subjects", "actions", "setting", "props", "framing",
            "subjectCoverage", "cameraAngle", "composition", "perspective",
            "lensFeel", "cameraMovement", "movementIntensity", "brightness",
            "contrast", "lightingType", "colorTemperature", "dominantColor",
            "saturation", "depthOfField", "texture",
        ):
            assert field in shot["visual"], field
        assert isinstance(shot["components"]["texts"], list)
        assert isinstance(shot["components"]["compositingEvents"], str)

    def test_audio_and_editing_fields(self):
        svc = default_mock_unified(["SH0001"])
        audio = json.loads(svc.analyze_batch(_clips("SH0001"), _group("audio")))["shots"][0]
        assert set(audio["audio"]) == {"speech", "bgmStyle", "soundEffects"}
        editing = json.loads(
            svc.analyze_batch(_clips("SH0001"), _group("editing_function"))
        )["shots"][0]
        assert set(editing["function"]) == {"sourceMedium", "subjectEmotion", "shotTone"}
        assert set(editing["editing"]) == {"transition", "continuity"}
        assert editing["confidence"]["overall"] in {"high", "medium", "low", "unknown"}

    def test_groups_do_not_overlap(self):
        owners: dict[str, str] = {}
        for name, owned in GROUP_OWNED_SECTIONS.items():
            for section in owned:
                assert section not in owners or section == "confidence"
                owners[section] = name
