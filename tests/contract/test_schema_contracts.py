"""契约测试：fixtures 必须通过 schema，编程派生的非法变体必须失败。

合法基准为 tests/fixtures/output_full 与 tests/fixtures/minimal；
每个 artifact 至少 3 个非法变体（缺 required 字段 / 错误类型 /
非法枚举 / 非法 ID / 越界时间）由合法夹具深拷贝后破坏派生。
"""

from __future__ import annotations

import pytest

from conftest import ARTIFACT_FILES, Mutation, delete, load_fixture, mutate

from memoloupe.artifacts.schemas import ArtifactName
from memoloupe.validate.json_contracts import validate_file

FIXTURE_DIRS = ["output_full", "minimal"]


@pytest.mark.parametrize("fixture_dir", FIXTURE_DIRS)
@pytest.mark.parametrize(("name", "rel"), ARTIFACT_FILES)
def test_fixture_matches_schema(fixture_dir: str, name: str, rel: str) -> None:
    data = load_fixture(fixture_dir, rel)
    issues = validate_file(ArtifactName(name), data)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == [], "\n".join(str(i) for i in errors)


# 每个 artifact 的非法变体：(标签, 破坏函数)。
MUTATIONS: dict[str, list[tuple[str, Mutation]]] = {
    "media": [
        ("missing-revisionID", lambda d: delete(d, "source.revisionID")),
        ("durationMs-wrong-type", lambda d: mutate(d, "source.durationMs", "9800")),
        ("coverage-illegal-status",
         lambda d: mutate(d, "source.analysisCoverage[0].status", "done")),
    ],
    "shots": [
        ("missing-shotID", lambda d: delete(d, "shots[0].shotID")),
        ("method-not-const", lambda d: mutate(d, "analysis.method", "otherMethod")),
        ("illegal-shot-id", lambda d: mutate(d, "shots[0].shotID", "SH001")),
        ("boundary-illegal-confidence",
         lambda d: mutate(d, "boundaries[0].confidence", "extreme")),
    ],
    "audio-cuts": [
        ("illegal-status", lambda d: mutate(d, "status", "partial")),
        ("bad-audio-boundary-id",
         lambda d: mutate(d, "boundaries[0].audioBoundaryID", "AU123")),
        ("illegal-classification",
         lambda d: mutate(d, "shots[0].boundaryIn.classification", "jCut")),
        ("missing-syncTolerance", lambda d: delete(d, "analysis.syncToleranceMs")),
    ],
    "frame-evidence": [
        ("bad-evidence-id",
         lambda d: mutate(d, "frames[0].evidenceID", "X_SH0001_MAIN")),
        ("illegal-frameType", lambda d: mutate(d, "frames[0].frameType", "main")),
        ("missing-request-width", lambda d: delete(d, "request.width")),
    ],
    "asr": [
        ("service-not-const", lambda d: mutate(d, "service", "whisper")),
        ("illegal-status", lambda d: mutate(d, "status", "partial")),
        ("missing-transcript", lambda d: delete(d, "transcript")),
    ],
    "music-flags": [
        ("illegal-state", lambda d: mutate(d, "shots[0].state", "noisy")),
        ("overlap-out-of-range",
         lambda d: mutate(d, "shots[0].musicOverlapRatio", 1.5)),
        ("missing-stateTally", lambda d: delete(d, "stateTally")),
    ],
    "unified-media": [
        ("service-not-const", lambda d: mutate(d, "service", "other")),
        ("external-frame-extraction-true",
         lambda d: mutate(d, "request.externalFrameExtraction", True)),
        ("missing-visual-framing",
         lambda d: delete(d, "batches[0].response.shots[0].visual.framing")),
        ("illegal-shot-status",
         lambda d: mutate(d, "shotStatuses.SH0001", "done")),
    ],
    "camera-motion": [
        ("missing-capabilityStatus",
         lambda d: delete(d, "analysis.capabilityStatus")),
        ("illegal-cameraMovement",
         lambda d: mutate(d, "shots[0].cameraMovement", "dolly")),
        ("illegal-intensity",
         lambda d: mutate(d, "shots[0].movementIntensity", "extreme")),
    ],
    "quality-flags": [
        ("illegal-audioStatus", lambda d: mutate(d, "audioStatus", "unknown")),
        ("illegal-flag", lambda d: mutate(d, "shots[0].flags", ["色彩断层"])),
        ("missing-method", lambda d: delete(d, "method")),
    ],
    "audio-energy": [
        ("illegal-label", lambda d: mutate(d, "shots[0].label", "爆表")),
        ("hasAudio-wrong-type", lambda d: mutate(d, "hasAudio", "yes")),
        ("missing-sampleRate", lambda d: delete(d, "sampleRate")),
    ],
    "story-blocks": [
        ("illegal-divisionAxis",
         lambda d: mutate(d, "blocks[0].divisionAxis", "时间")),
        ("bad-block-id", lambda d: mutate(d, "blocks[0].storyBlockID", "B001")),
        ("bad-slot-id", lambda d: mutate(d, "slots[0].slotID", "S0001")),
        ("illegal-informationRole",
         lambda d: mutate(d, "blocks[0].informationRole", "闲聊")),
    ],
    "style-profile": [
        ("schemaVersion-not-2", lambda d: mutate(d, "schemaVersion", 1)),
        ("illegal-narrativeFunction",
         lambda d: mutate(d, "structure.slots[0].L1.narrativeFunction", "climax")),
        ("missing-distillStatus", lambda d: delete(d, "distillStatus")),
        ("bad-l3-shot-id",
         lambda d: mutate(d, "structure.slots[0].L3.shotIds", ["SH01"])),
    ],
}

_VARIANT_PARAMS = [
    pytest.param(name, label, id=f"{name}:{label}")
    for name, variants in MUTATIONS.items()
    for label, _ in variants
]


@pytest.mark.parametrize(("name", "label"), _VARIANT_PARAMS)
def test_invalid_variants_fail_schema(name: str, label: str) -> None:
    rel = dict(ARTIFACT_FILES)[name]
    data = load_fixture("output_full", rel)
    broken = dict(MUTATIONS[name])[label](data)
    issues = validate_file(ArtifactName(name), broken)
    errors = [i for i in issues if i.severity == "error"]
    assert errors, f"变体 {name}:{label} 未被 schema 拒绝"


def test_validate_file_reports_all_errors() -> None:
    """iter_errors 必须一次报出全部错误，而不是第一个就停。"""
    data = load_fixture("output_full", "raw/media.json")
    broken = mutate(data, "source.durationMs", "9800")
    broken = mutate(broken, "source.durationSec", "9.8")
    issues = validate_file(ArtifactName.MEDIA, broken)
    paths = {i.json_path for i in issues}
    assert "$.source.durationMs" in paths
    assert "$.source.durationSec" in paths
    assert len(issues) >= 2
