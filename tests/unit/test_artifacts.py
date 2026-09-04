"""artifacts 层（schemas / store / manifest / migrations）单元测试。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from memoloupe.artifacts.manifest import (
    load_manifest,
    record_artifact,
    set_source_revision,
)
from memoloupe.artifacts.migrations import (
    migrate_legacy_names,
    migrate_unified_media_v1,
    upgrade_unified_media_v1_to_v2,
)
from memoloupe.artifacts.schemas import (
    SCHEMA_DIR,
    ArtifactName,
    load_schema,
    schema_path,
    validate_artifact,
)
from memoloupe.artifacts.store import ArtifactStore, WriteMetadata
from memoloupe.core.errors import ContractError

# ---------------------------------------------------------------------------
# 最小合法夹具（已对照 schemas/ 校验）
# ---------------------------------------------------------------------------


def minimal_media() -> dict:
    return {
        "source": {
            "assetID": "asset-001",
            "sourcePath": "input.mp4",
            "revisionID": "a1b2c3d4e5f6",
            "durationMs": 10000,
            "durationSec": 10.0,
            "frameRate": 25.0,
            "resolution": {"width": 1920, "height": 1080},
            "aspectRatio": 16 / 9,
            "audioTracks": [],
            "analyzedRange": {"startMs": 0, "endMs": 10000},
            "analysisCoverage": [],
        }
    }


def minimal_shots() -> dict:
    return {
        "analysis": {
            "method": "memoClipHardCutCandidateCuts",
            "fps": 2.0,
            "sourceFps": 25.0,
            "durationMs": 10000,
            "selectedBoundaryCount": 0,
        },
        "boundaries": [],
        "shots": [],
    }


def minimal_style_profile() -> dict:
    return {
        "schemaVersion": 2,
        "id": "profile-001",
        "createdAt": "2026-08-23T00:00:00Z",
        "source": {
            "videoTitle": "demo",
            "videoPath": "input.mp4",
            "durationSeconds": 10.0,
            "shotAnalysisPath": "shot-analysis.html",
            "storyAnalysisPath": "shot-analysis.html",
            "sourceRevision": "a1b2c3d4e5f6",
        },
        "structure": {
            "slots": [],
            "hook": None,
            "payoff": None,
            "turns": [],
            "nonLinearDevices": [],
            "expectationChains": [],
        },
        "pacing": {
            "shotDuration": {"avgSeconds": 5.0},
            "densityCurve": [],
            "slotPacing": [],
            "audioBoundaryBySlot": [],
            "musicAlignment": "unknown",
        },
        "style": {},
        "structureRequirements": [],
        "adoptionHints": {
            "strengths": [],
            "cautions": [],
            "suggestedDefault": "none",
        },
        "discussionItems": [],
        "asrTextStats": {
            "segmentCount": 0,
            "characterCount": 0,
            "speechDurationMs": 0,
        },
        "distillStatus": "skipped",
    }


@pytest.fixture()
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path)


# ---------------------------------------------------------------------------
# schema 注册表
# ---------------------------------------------------------------------------


class TestSchemaRegistry:
    def test_schema_dir_points_to_repo_schemas(self) -> None:
        assert SCHEMA_DIR.is_dir()
        assert (SCHEMA_DIR / "media.json").is_file()

    def test_all_twelve_names_load(self) -> None:
        # 命名需保持：固定 12 个 Phase 1/2 artifact + motion-effects（05-07）后
        # 共 13 个；此处不断言具体数量，而是要求每个枚举都有可加载的 schema。
        assert len(list(ArtifactName)) == 13
        for name in ArtifactName:
            schema = load_schema(name)
            assert isinstance(schema, dict)
            assert schema_path(name).is_file()
        assert ArtifactName.MOTION_EFFECTS in set(ArtifactName)

    def test_load_schema_is_cached(self) -> None:
        assert load_schema(ArtifactName.MEDIA) is load_schema(ArtifactName.MEDIA)

    def test_validate_accepts_minimal_media(self) -> None:
        validate_artifact(ArtifactName.MEDIA, minimal_media())

    def test_validate_rejects_missing_required(self) -> None:
        with pytest.raises(ContractError) as exc_info:
            validate_artifact(ArtifactName.MEDIA, {"source": {}})
        err = exc_info.value
        assert err.artifact == "media"
        assert err.json_path.startswith("$")

    def test_validate_rejects_bad_enum(self) -> None:
        data = minimal_media()
        data["source"]["analysisCoverage"] = [
            {"capability": "asr", "status": "bogus"}
        ]
        with pytest.raises(ContractError):
            validate_artifact(ArtifactName.MEDIA, data)


# ---------------------------------------------------------------------------
# ArtifactStore
# ---------------------------------------------------------------------------


class TestArtifactStore:
    def test_write_read_roundtrip(self, store: ArtifactStore) -> None:
        data = minimal_media()
        store.write(
            ArtifactName.MEDIA, data, WriteMetadata(fingerprint="fp-1")
        )
        assert store.read(ArtifactName.MEDIA) == data

    def test_path_layout(self, store: ArtifactStore, tmp_path: Path) -> None:
        assert store.path(ArtifactName.MEDIA) == tmp_path / "raw" / "media.json"
        assert store.path(ArtifactName.SHOTS) == tmp_path / "raw" / "shots.json"
        # 唯一例外：style-profile 在根目录
        assert (
            store.path(ArtifactName.STYLE_PROFILE)
            == tmp_path / "style-profile.json"
        )

    def test_write_invalid_raises_and_leaves_no_file(
        self, store: ArtifactStore
    ) -> None:
        with pytest.raises(ContractError):
            store.write(
                ArtifactName.MEDIA, {}, WriteMetadata(fingerprint="fp-bad")
            )
        assert not store.path(ArtifactName.MEDIA).exists()
        # manifest 也不应记录该 artifact
        manifest = load_manifest(store.root)
        assert "media" not in manifest["artifacts"]

    def test_style_profile_written_at_root(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        store.write(
            ArtifactName.STYLE_PROFILE,
            minimal_style_profile(),
            WriteMetadata(fingerprint="fp-sp"),
        )
        assert (tmp_path / "style-profile.json").is_file()
        assert store.read(ArtifactName.STYLE_PROFILE)["schemaVersion"] == 2

    def test_exists_and_status(self, store: ArtifactStore) -> None:
        assert not store.exists(ArtifactName.MEDIA)
        assert store.status(ArtifactName.MEDIA) is None
        store.write(
            ArtifactName.MEDIA,
            minimal_media(),
            WriteMetadata(fingerprint="fp-1", status="partial"),
        )
        assert store.exists(ArtifactName.MEDIA)
        assert store.status(ArtifactName.MEDIA) == "partial"

    def test_is_reusable_with_matching_fingerprint(
        self, store: ArtifactStore
    ) -> None:
        store.write(
            ArtifactName.MEDIA,
            minimal_media(),
            WriteMetadata(fingerprint="fp-1"),
        )
        assert store.is_reusable(ArtifactName.MEDIA, "fp-1")

    def test_is_reusable_fingerprint_mismatch(
        self, store: ArtifactStore
    ) -> None:
        store.write(
            ArtifactName.MEDIA,
            minimal_media(),
            WriteMetadata(fingerprint="fp-1"),
        )
        assert not store.is_reusable(ArtifactName.MEDIA, "fp-other")

    def test_is_reusable_missing_file(self, store: ArtifactStore) -> None:
        assert not store.is_reusable(ArtifactName.MEDIA, "fp-1")

    def test_is_reusable_corrupted_file(self, store: ArtifactStore) -> None:
        store.write(
            ArtifactName.MEDIA,
            minimal_media(),
            WriteMetadata(fingerprint="fp-1"),
        )
        store.path(ArtifactName.MEDIA).write_text("not json", encoding="utf-8")
        assert not store.is_reusable(ArtifactName.MEDIA, "fp-1")

    def test_is_reusable_schema_invalid_file(
        self, store: ArtifactStore
    ) -> None:
        store.write(
            ArtifactName.MEDIA,
            minimal_media(),
            WriteMetadata(fingerprint="fp-1"),
        )
        # 绕过 store 直接写入 schema 不合法内容
        store.path(ArtifactName.MEDIA).write_text(
            json.dumps({"unexpected": True}), encoding="utf-8"
        )
        assert not store.is_reusable(ArtifactName.MEDIA, "fp-1")

    def test_is_reusable_requires_complete_status(
        self, store: ArtifactStore
    ) -> None:
        store.write(
            ArtifactName.MEDIA,
            minimal_media(),
            WriteMetadata(fingerprint="fp-1", status="partial"),
        )
        assert not store.is_reusable(ArtifactName.MEDIA, "fp-1")

    def test_legacy_shot_candidates_read_mapping(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        (raw_dir / "shot-candidates.json").write_text(
            json.dumps(minimal_shots()), encoding="utf-8"
        )
        data = store.read(ArtifactName.SHOTS)
        assert data["analysis"]["method"] == "memoClipHardCutCandidateCuts"

    def test_canonical_shots_preferred_over_legacy(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        store.write(
            ArtifactName.SHOTS,
            minimal_shots(),
            WriteMetadata(fingerprint="fp-new"),
        )
        legacy = dict(minimal_shots())
        legacy["analysis"]["durationMs"] = 99999
        (tmp_path / "raw" / "shot-candidates.json").write_text(
            json.dumps(legacy), encoding="utf-8"
        )
        assert store.read(ArtifactName.SHOTS)["analysis"]["durationMs"] == 10000

    def test_read_missing_raises_contract_error(
        self, store: ArtifactStore
    ) -> None:
        with pytest.raises(ContractError):
            store.read(ArtifactName.MEDIA)


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


class TestManifest:
    def test_load_missing_returns_skeleton(self, tmp_path: Path) -> None:
        manifest = load_manifest(tmp_path)
        assert manifest == {
            "manifestVersion": 1,
            "sourceRevisionID": None,
            "artifacts": {},
        }

    def test_record_artifact_roundtrip(self, tmp_path: Path) -> None:
        record_artifact(
            tmp_path,
            ArtifactName.SHOTS,
            path="raw/shots.json",
            fingerprint="fp-1",
            status="complete",
            legacy_paths=("raw/shot-candidates.json",),
        )
        entry = load_manifest(tmp_path)["artifacts"]["shots"]
        assert entry["path"] == "raw/shots.json"
        assert entry["legacyPaths"] == ["raw/shot-candidates.json"]
        assert entry["schemaVersion"] == "1.0"
        assert entry["fingerprint"] == "fp-1"
        assert entry["status"] == "complete"
        # updatedAt 是可解析的 UTC ISO-8601
        parsed = datetime.fromisoformat(
            entry["updatedAt"].replace("Z", "+00:00")
        )
        assert parsed.tzinfo is not None

    def test_record_artifact_overwrites_same_name(
        self, tmp_path: Path
    ) -> None:
        record_artifact(
            tmp_path, ArtifactName.MEDIA,
            path="raw/media.json", fingerprint="fp-1", status="complete",
        )
        record_artifact(
            tmp_path, ArtifactName.MEDIA,
            path="raw/media.json", fingerprint="fp-2", status="partial",
        )
        artifacts = load_manifest(tmp_path)["artifacts"]
        assert len(artifacts) == 1
        assert artifacts["media"]["fingerprint"] == "fp-2"
        assert artifacts["media"]["status"] == "partial"

    def test_set_source_revision(self, tmp_path: Path) -> None:
        set_source_revision(tmp_path, "a1b2c3d4e5f6")
        assert load_manifest(tmp_path)["sourceRevisionID"] == "a1b2c3d4e5f6"

    def test_store_write_updates_manifest(self, store: ArtifactStore) -> None:
        store.write(
            ArtifactName.SHOTS,
            minimal_shots(),
            WriteMetadata(fingerprint="fp-9"),
        )
        entry = load_manifest(store.root)["artifacts"]["shots"]
        assert entry["path"] == "raw/shots.json"
        assert entry["legacyPaths"] == ["raw/shot-candidates.json"]
        assert entry["fingerprint"] == "fp-9"
        assert entry["status"] == "complete"


# ---------------------------------------------------------------------------
# migrations
# ---------------------------------------------------------------------------


class TestMigrations:
    @staticmethod
    def _unified_v1() -> dict:
        return {
            "service": "unifiedAudioVideo",
            "schemaFingerprint": "legacy-fingerprint",
            "request": {},
            "retryPolicy": {},
            "clips": [],
            "batches": [
                {
                    "batchID": "B0001",
                    "shotIDs": ["SH0001"],
                    "status": "complete",
                    "response": {
                        "shots": [
                            {
                                "shotID": "SH0001",
                                "visual": {
                                    "content": "旅客在机场出发",
                                    "subjects": "旅客",
                                    "actions": "行走",
                                    "setting": "机场",
                                    "props": "行李箱",
                                    "framing": "全景",
                                    "subjectCoverage": "全身",
                                    "cameraAngle": "平视",
                                    "composition": "居中",
                                    "perspective": "第三人称观察",
                                    "lensFeel": "标准感",
                                    "cameraMovement": "固定",
                                    "movementIntensity": "static",
                                    "brightness": "明亮",
                                    "contrast": "中",
                                    "lightingType": "自然光",
                                    "colorTemperature": "中性",
                                    "dominantColor": "蓝色",
                                    "saturation": "中",
                                    "depthOfField": "深景深",
                                    "texture": "清晰",
                                },
                                "function": {
                                    "sourceMedium": "实拍素材",
                                    "subjectEmotion": "平静",
                                    "shotTone": "沉稳",
                                },
                                "audio": {
                                    "speech": "准备出发",
                                    "bgmStyle": "轻快",
                                    "soundEffects": "环境声",
                                },
                                "components": {
                                    "texts": [],
                                    "compositingEvents": "无",
                                },
                                "editing": {
                                    "transition": "硬切",
                                    "continuity": "动作连续",
                                },
                                "confidence": {
                                    "visual": "high",
                                    "audio": "medium",
                                    "editing": "low",
                                    "overall": "medium",
                                },
                            }
                        ]
                    },
                }
            ],
            "shotStatuses": {"SH0001": "succeeded"},
            "status": "complete",
        }

    def test_unified_v1_upgrade_is_explicit_and_preserves_legacy_values(self) -> None:
        upgraded = upgrade_unified_media_v1_to_v2(self._unified_v1())
        shot = upgraded["batches"][0]["response"]["shots"][0]
        assert upgraded["schemaVersion"] == 2
        assert upgraded["schemaFingerprint"] != "legacy-fingerprint"
        assert "content" not in shot["visual"]
        assert "subjectCoverage" not in shot["visual"]
        assert "movementIntensity" not in shot["visual"]
        assert shot["visual"]["viewpoint"] == "第三人称观察"
        assert shot["visual"]["perceivedLensFeel"] == "标准感"
        assert shot["visual"]["lightingSource"] == "自然光"
        assert shot["visual"]["perceivedColorTemperature"] == "中性"
        assert shot["visual"]["imageTexture"] == "清晰"
        assert "speech" not in shot["audio"]
        assert shot["audio"]["soundEvents"] == "环境声"
        assert "editing" not in shot
        assert shot["confidence"] == {
            "visual": "high",
            "audio": "medium",
            "function": "low",
        }
        assert shot["extensions"]["legacyV1"]["visual.content"] == "旅客在机场出发"
        assert shot["extensions"]["legacyV1"]["audio.speech"] == "准备出发"
        assert shot["extensions"]["legacyV1"]["editing.transition"] == "硬切"

    def test_unified_file_migration_keeps_v1_backup_and_is_idempotent(
        self, tmp_path: Path
    ) -> None:
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        current = raw_dir / "unified-media.json"
        current.write_text(json.dumps(self._unified_v1()), encoding="utf-8")

        assert migrate_unified_media_v1(tmp_path)
        assert (raw_dir / "unified-media.v1.json").is_file()
        assert json.loads(current.read_text())["schemaVersion"] == 2
        assert migrate_unified_media_v1(tmp_path) is None

    def test_legacy_shot_candidates_copied(self, tmp_path: Path) -> None:
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        legacy = raw_dir / "shot-candidates.json"
        legacy.write_text(json.dumps(minimal_shots()), encoding="utf-8")

        done = migrate_legacy_names(tmp_path)
        assert len(done) == 1
        assert (raw_dir / "shots.json").is_file()
        # 复制而非移动：旧文件保留作为备份
        assert legacy.is_file()
        assert json.loads((raw_dir / "shots.json").read_text()) == json.loads(
            legacy.read_text()
        )

    def test_idempotent_when_new_name_exists(self, tmp_path: Path) -> None:
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        (raw_dir / "shot-candidates.json").write_text("{}", encoding="utf-8")
        (raw_dir / "shots.json").write_text(
            json.dumps(minimal_shots()), encoding="utf-8"
        )
        assert migrate_legacy_names(tmp_path) == []

    def test_noop_when_nothing_to_migrate(self, tmp_path: Path) -> None:
        assert migrate_legacy_names(tmp_path) == []

    def test_second_run_is_noop(self, tmp_path: Path) -> None:
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        (raw_dir / "shot-candidates.json").write_text(
            json.dumps(minimal_shots()), encoding="utf-8"
        )
        migrate_legacy_names(tmp_path)
        assert migrate_legacy_names(tmp_path) == []
