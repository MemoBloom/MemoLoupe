"""ShotAnalysisPipeline 编排单元测试（docs/03 §1/§5/§6）。

媒体步骤全部用 monkeypatch 替换为 schema 合法的假实现，只测试编排语义：
指纹复用、force/no-cache、锁冲突与接管、必需/非必需失败、stub 产物契约。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from memoloupe.analysis import shot_pipeline
from memoloupe.analysis.shot_pipeline import (
    ShotAnalysisPipeline,
    ShotAnalysisRequest,
    build_asr_stub,
    build_audio_cuts_stub,
    build_camera_motion_stub,
    build_motion_effects_stub,
    build_music_flags_stub,
    build_unified_media_stub,
)
from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
from memoloupe.core.config import DEFAULT_CONFIG, load_config


def _fake_media(source: Path, config: dict, *, pool=None, analyzed_range=None) -> dict:
    start_ms, end_ms = analyzed_range or (0, 2000)
    return {
        "source": {
            "assetID": Path(source).stem,
            "sourcePath": str(source),
            "revisionID": "fakefakefake",
            "durationMs": 2000,
            "durationSec": 2.0,
            "frameRate": 10.0,
            "resolution": {"width": 320, "height": 240},
            "aspectRatio": 320 / 240,
            "audioTracks": [],
            "analyzedRange": {"startMs": start_ms, "endMs": end_ms},
            "analysisCoverage": [{"capability": "mediaMetadata", "status": "complete"}],
        }
    }


def _fake_shots(source: Path, media: dict, config: dict, *, pool=None) -> dict:
    arange = media["source"]["analyzedRange"]
    start_ms, end_ms = arange["startMs"], arange["endMs"]
    return {
        "analysis": {
            "method": "memoClipHardCutCandidateCuts",
            "algorithmVersion": "shots.v1",
            "fps": 2.0,
            "sourceFps": 10.0,
            "durationMs": 2000,
            "selectedBoundaryCount": 0,
        },
        "boundaries": [],
        "shots": [
            {
                "shotID": "SH0001",
                "sequenceIndex": 1,
                "detectedStartMs": start_ms,
                "detectedEndMs": end_ms,
                "finalStartMs": start_ms,
                "finalEndMs": end_ms,
                "durationMs": end_ms - start_ms,
                "boundaryIn": {"type": "sourceStart", "confidence": "high", "metric": None},
                "boundaryOut": {"type": "sourceEnd", "confidence": "high", "metric": None},
                "needsReview": False,
            }
        ],
    }


def _fake_frames(source, shots, media, config, out_dir, *, pool) -> dict:
    return {
        "status": "complete",
        "version": "frames.v1",
        "request": {
            "sourceRevisionID": "fakefakefake",
            "inputVideo": str(source),
            "inputCacheKey": "original-fake",
            "width": 640,
        },
        "extraction": {"mode": "auto", "workerCount": 1, "cachedFrames": 0},
        "frames": [
            {
                "evidenceID": "F_SH0001_MAIN",
                "frameID": "F_SH0001_MAIN",
                "shotID": "SH0001",
                "frameType": "representative",
                "timeMs": 1000,
                "range": {"startMs": 1000, "endMs": 1000},
                "fileRef": "evidence/frames/F_SH0001_MAIN.jpg",
                "quality": "usable",
                "summary": "fake",
            }
        ],
        "failedFrames": [],
    }


def _fake_clips(source, shots, has_audio, config, out_dir, *, pool) -> list[dict]:
    out_dir = Path(out_dir)
    for shot in shots:
        rel = f"clips/{shot['shotID']}.mp4"
        target = out_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake")
    return [
        {
            "shotID": shot["shotID"],
            "startMs": shot["finalStartMs"],
            "endMs": shot["finalEndMs"],
            "durationMs": shot["durationMs"],
            "file": f"clips/{shot['shotID']}.mp4",
            "modelFile": f"clips/model-proxy/{shot['shotID']}-fake.mp4",
            "modelDurationMs": shot["durationMs"],
            "modelNormalization": None,
        }
        for shot in shots
    ]


def _fake_energy(source, shots, has_audio, config, *, pool) -> dict:
    return {
        "source": str(source),
        "durationMs": 2000,
        "sampleRate": 16000,
        "hasAudio": False,
        "thresholds": {},
        "shots": [
            {"shotID": s["shotID"], "label": "unknown", "medianDb": None, "frameCount": 0}
            for s in shots
        ],
    }


def _fake_audio_cuts(source, shots, media, config, *, pool=None, align_boundaries=False) -> dict:
    """shots 参数是 shots.json 文档 dict（与真实 detect_audio_cuts 签名一致）。"""
    doc = build_audio_cuts_stub(shots.get("shots", []), config)
    doc["movedBoundaries"] = []
    return doc


def _fake_music(source, shots, asr, media, config, *, pool=None) -> dict:
    return build_music_flags_stub(shots, config)


def _fake_asr_stage(source, media, config, service=None) -> dict:
    return build_asr_stub()


def _fake_camera_motion(source, shots, media, config, *, pool=None) -> dict:
    return build_camera_motion_stub(shots, media, config)


def _fake_review_timeline(source, media, config, *, pool=None) -> dict:
    """确定性索引 fake：无 ffprobe 依赖的完整文档（单元夹具用）。"""
    analyzed = media.get("analyzedRange", {"startMs": 0, "endMs": 1000})
    start, end = int(analyzed["startMs"]), int(analyzed["endMs"])
    span = max(end - start, 1)
    pts = sorted({start, start + span // 2, end - 1})
    return {
        "schemaVersion": 1,
        "status": "complete",
        "sourceRevisionID": media.get("source", {}).get("revisionID", ""),
        "analysis": {
            "method": "fake",
            "algorithmVersion": "review-timeline.v1",
            "analyzedRange": {"startMs": start, "endMs": end},
        },
        "videoFrames": {
            "status": "complete",
            "timingMode": "pts-index",
            "frameCount": len(pts),
            "ptsMs": pts,
        },
        "waveform": {
            "status": "unavailable",
            "channelMode": "unavailable",
            "reason": "fake：无音轨",
        },
    }


def _fake_shot_relations(source, shots, config, out_dir, **kwargs) -> dict:
    """切点关系 fake：N-1 个 pair、确定性指标落 unknown（单元夹具用）。"""
    from memoloupe.analysis.shot_relations import build_shot_relations_stub

    doc = build_shot_relations_stub(
        shots, config, kwargs.get("source_revision_id", ""), "fake"
    )
    doc["status"] = "complete"
    return doc


def _fake_quality(source, shots, has_audio, config, *, pool) -> dict:
    return {
        "status": "complete",
        "version": "quality.v1",
        "method": "fake",
        "audioStatus": "absent",
        "flaggedShotCount": 0,
        "shotCount": len(shots),
        "thresholds": {},
        "shots": [
            {
                "shotID": s["shotID"],
                "startMs": s["finalStartMs"],
                "endMs": s["finalEndMs"],
                "flags": [],
                "confidence": "unknown",
                "measurements": {},
            }
            for s in shots
        ],
    }


def _fake_motion_effects(source, shots, media, config, *, pool) -> dict:
    # 编排测试不需要候选；返回 complete 的空候选文档（shots 摘要仍覆盖全镜头）。
    doc = build_motion_effects_stub(shots, media, config)
    doc["status"] = "complete"
    doc["analysis"]["note"] = "unit-test fake"
    return doc


def _fake_render(out_dir, *, status="draft") -> Path:
    target = Path(out_dir) / "shot-analysis.html"
    target.write_text("<html>fake</html>", encoding="utf-8")
    return target


@pytest.fixture
def source(tmp_path) -> Path:
    path = tmp_path / "input.mp4"
    path.write_bytes(b"fake video bytes")
    return path


@pytest.fixture
def patched(monkeypatch):
    """把全部媒体/渲染/校验步骤替换为假实现，返回调用计数器。"""
    calls: dict[str, int] = {}

    def counted(name, fn):
        def wrapper(*args, **kwargs):
            calls[name] = calls.get(name, 0) + 1
            return fn(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(
        shot_pipeline, "probe_media", counted("probe_media", _fake_media)
    )
    monkeypatch.setattr(
        shot_pipeline, "detect_shots", counted("detect_shots", _fake_shots)
    )
    monkeypatch.setattr(
        shot_pipeline, "extract_frames", counted("extract_frames", _fake_frames)
    )
    monkeypatch.setattr(
        shot_pipeline, "build_clips", counted("build_clips", _fake_clips)
    )
    monkeypatch.setattr(
        shot_pipeline,
        "detect_audio_energy",
        counted("detect_audio_energy", _fake_energy),
    )
    monkeypatch.setattr(
        shot_pipeline, "detect_quality", counted("detect_quality", _fake_quality)
    )
    monkeypatch.setattr(
        shot_pipeline,
        "detect_motion_effects",
        counted("detect_motion_effects", _fake_motion_effects),
    )
    monkeypatch.setattr(
        shot_pipeline,
        "detect_audio_cuts",
        counted("detect_audio_cuts", _fake_audio_cuts),
    )
    monkeypatch.setattr(
        shot_pipeline, "detect_music", counted("detect_music", _fake_music)
    )
    monkeypatch.setattr(
        shot_pipeline, "run_asr_stage", counted("run_asr_stage", _fake_asr_stage)
    )
    monkeypatch.setattr(
        shot_pipeline,
        "analyze_camera_motion",
        counted("analyze_camera_motion", _fake_camera_motion),
    )
    monkeypatch.setattr(
        shot_pipeline,
        "build_review_timeline",
        counted("build_review_timeline", _fake_review_timeline),
    )
    monkeypatch.setattr(
        shot_pipeline,
        "build_shot_relations",
        counted("build_shot_relations", _fake_shot_relations),
    )
    monkeypatch.setattr(shot_pipeline, "render_shot_html", _fake_render)
    monkeypatch.setattr(shot_pipeline, "validate_output_dir", lambda root, *, strict=False: [])
    monkeypatch.setattr(shot_pipeline, "validate_html", lambda path, *, root=None, strict=False: [])
    monkeypatch.setattr(
        shot_pipeline,
        "_tool_versions",
        lambda config: {"ffmpeg": "fake-ffmpeg 8.1", "ffprobe": "fake-ffprobe 8.1"},
    )
    return calls


def _request(source: Path, out_dir: Path, **kwargs) -> ShotAnalysisRequest:
    return ShotAnalysisRequest(source=source, output_dir=out_dir, **kwargs)


def _step(report, name: str):
    return next(s for s in report.steps if s.name == name)


class TestHappyPath:
    def test_first_run_completes_and_writes_all_artifacts(self, source, tmp_path, patched):
        out_dir = tmp_path / "out"
        report = ShotAnalysisPipeline().run(_request(source, out_dir))

        assert report.phase == "shot"
        assert report.status == "complete"
        assert report.elapsed_ms >= 0
        names = [s.name for s in report.steps]
        assert names == [
            "acquire_lock",
            "probe_media",
            "detect_shots",
            "detect_audio_cuts",
            "run_asr",
            "detect_music",
            "extract_frames",
            "build_clips",
            "detect_audio_energy",
            "detect_quality",
            "detect_motion_effects",
            "unified_media_analysis",
            "analyze_camera_motion",
            "build_review_timeline",
            "build_shot_relations",
            "render_shot_html",
            "validate",
        ]
        assert _step(report, "acquire_lock").status == "complete"
        assert all(
            _step(report, n).status == "complete"
            for n in (
                "probe_media",
                "detect_shots",
                "extract_frames",
                "build_clips",
                "detect_audio_energy",
                "detect_quality",
                "detect_motion_effects",
                "build_review_timeline",
                "build_shot_relations",
                "render_shot_html",
                "validate",
            )
        )
        # 显式降级步骤：状态来自产物内嵌状态，整体仍 complete
        assert _step(report, "detect_audio_cuts").status == "unavailable"
        assert _step(report, "run_asr").status == "skipped"
        assert _step(report, "detect_music").status == "skipped"
        assert _step(report, "unified_media_analysis").status == "skipped"
        assert _step(report, "analyze_camera_motion").status == "unavailable"
        # Phase 06：确定性审片索引与切点关系默认运行
        assert _step(report, "build_review_timeline").status == "complete"
        assert _step(report, "build_shot_relations").status == "complete"
        # 所有步骤计时非负
        assert all(s.elapsed_ms >= 0 for s in report.steps)
        # 产物落盘（raw/*.json + HTML + manifest）
        for artifact in (
            "media", "shots", "frame-evidence", "audio-energy", "quality-flags",
            "asr", "music-flags", "audio-cuts", "camera-motion", "unified-media",
            "motion-effects", "review-timeline", "shot-relations",
        ):
            assert (out_dir / "raw" / f"{artifact}.json").is_file(), artifact
            assert f"raw/{artifact}.json" in report.artifacts
        assert (out_dir / "shot-analysis.html").is_file()
        assert "shot-analysis.html" in report.artifacts
        # stub 状态在报告里可见
        assert any("降级" in w or "stub" in w.lower() for w in report.warnings)
        # 锁已释放
        assert not (out_dir / ".memoloupe.lock").exists()
        d = report.to_dict()
        assert d["phase"] == "shot" and d["status"] == "complete"
        json.dumps(d, ensure_ascii=False)  # 可序列化


class TestCacheReuse:
    def test_second_run_reuses_everything_except_lock(self, source, tmp_path, patched):
        out_dir = tmp_path / "out"
        pipeline = ShotAnalysisPipeline()
        first = pipeline.run(_request(source, out_dir))
        assert first.status == "complete"

        calls_before = dict(patched)
        second = pipeline.run(_request(source, out_dir))
        assert second.status == "complete"
        # 没有任何步骤重新执行
        assert patched == calls_before
        for step in second.steps:
            if step.name == "acquire_lock":
                assert step.status == "complete"
            else:
                assert step.status == "reused", step.name

    def test_force_step_reruns_only_that_step(self, source, tmp_path, patched):
        out_dir = tmp_path / "out"
        pipeline = ShotAnalysisPipeline()
        pipeline.run(_request(source, out_dir))
        calls_before = dict(patched)

        report = pipeline.run(
            _request(source, out_dir, force_steps=frozenset({"detect_shots"}))
        )
        assert _step(report, "detect_shots").status == "complete"
        assert patched["detect_shots"] == calls_before["detect_shots"] + 1
        # 上游与下游指纹均未变（force 只跳过被指定步骤的复用判定）
        assert _step(report, "probe_media").status == "reused"
        assert patched["probe_media"] == calls_before["probe_media"]
        assert _step(report, "extract_frames").status == "reused"
        assert _step(report, "detect_quality").status == "reused"
        # 有产物步骤执行过 → render/validate 不复用
        assert _step(report, "render_shot_html").status == "complete"
        assert _step(report, "validate").status == "complete"

    def test_no_cache_ignores_reuse(self, source, tmp_path, patched):
        out_dir = tmp_path / "out"
        pipeline = ShotAnalysisPipeline()
        pipeline.run(_request(source, out_dir))

        report = pipeline.run(_request(source, out_dir, no_cache=True))
        for step in report.steps:
            if step.name == "acquire_lock":
                continue
            assert step.status != "reused", step.name
        assert patched["probe_media"] == 2

    def test_config_change_invalidates_only_dependents(self, source, tmp_path, patched):
        out_dir = tmp_path / "out"
        pipeline = ShotAnalysisPipeline()
        pipeline.run(_request(source, out_dir))

        config = load_config({"shots": {"analysisFps": 4.0}}, env={})
        report = pipeline.run(_request(source, out_dir, config=config))
        assert _step(report, "probe_media").status == "reused"
        assert _step(report, "detect_shots").status == "complete"
        # shots 指纹变化 → 链 shots 的下游全部重跑；run_asr 只链 media → 复用
        assert _step(report, "detect_audio_cuts").status == "unavailable"
        assert _step(report, "run_asr").status == "reused"
        assert _step(report, "detect_music").status == "skipped"
        assert _step(report, "extract_frames").status == "complete"
        assert _step(report, "analyze_camera_motion").status == "unavailable"

    def test_deleted_artifact_reruns_that_step_and_render(
        self, source, tmp_path, patched
    ):
        out_dir = tmp_path / "out"
        pipeline = ShotAnalysisPipeline()
        pipeline.run(_request(source, out_dir))
        (out_dir / "raw" / "quality-flags.json").unlink()

        report = pipeline.run(_request(source, out_dir))
        assert report.status == "complete"
        assert _step(report, "probe_media").status == "reused"
        assert _step(report, "detect_shots").status == "reused"
        assert _step(report, "detect_audio_cuts").status == "reused"
        assert _step(report, "run_asr").status == "reused"
        assert _step(report, "detect_music").status == "reused"
        assert _step(report, "unified_media_analysis").status == "reused"
        assert _step(report, "analyze_camera_motion").status == "reused"
        assert _step(report, "detect_quality").status == "complete"
        assert _step(report, "render_shot_html").status == "complete"
        assert _step(report, "validate").status == "complete"


class TestLock:
    def test_live_lock_fails_without_touching_outputs(self, source, tmp_path, patched):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        # 活锁：PID 为当前 pytest 进程
        (out_dir / ".memoloupe.lock").write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "host": "other-host",
                    "runID": "deadbeef",
                    "startedAt": "2026-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        report = ShotAnalysisPipeline().run(_request(source, out_dir))
        assert report.status == "failed"
        assert _step(report, "acquire_lock").status == "failed"
        assert any("锁" in w or "lock" in w.lower() for w in report.warnings)
        # 未写任何产物
        assert not (out_dir / "raw").exists()
        # 活锁不被接管
        assert json.loads(
            (out_dir / ".memoloupe.lock").read_text(encoding="utf-8")
        )["runID"] == "deadbeef"

    def test_stale_lock_is_taken_over(self, source, tmp_path, patched):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        #  stale 锁：已退出的进程 PID
        dead = subprocess.run(
            ["python3", "-c", "import os; print(os.getpid())"],
            capture_output=True,
            check=True,
            text=True,
        )
        dead_pid = int(dead.stdout.strip())
        (out_dir / ".memoloupe.lock").write_text(
            json.dumps(
                {
                    "pid": dead_pid,
                    "host": "old-host",
                    "runID": "stale000",
                    "startedAt": "2026-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        report = ShotAnalysisPipeline().run(_request(source, out_dir))
        assert report.status == "complete"
        assert _step(report, "acquire_lock").status == "complete"
        assert any("stale" in w.lower() or "陈旧" in w for w in report.warnings)
        assert not (out_dir / ".memoloupe.lock").exists()


class TestFailureSemantics:
    def test_required_step_failure_aborts(self, source, tmp_path, monkeypatch, patched):
        def boom(source, media, config, *, pool=None):
            raise RuntimeError("detect_shots exploded")

        monkeypatch.setattr(shot_pipeline, "detect_shots", boom)
        report = ShotAnalysisPipeline().run(_request(source, tmp_path / "out"))
        assert report.status == "failed"
        assert _step(report, "detect_shots").status == "failed"
        # 后续步骤未执行
        names = [s.name for s in report.steps]
        assert "extract_frames" not in names
        assert "validate" not in names

    def test_optional_step_failure_continues_as_partial(
        self, source, tmp_path, monkeypatch, patched
    ):
        def boom(source, shots, has_audio, config, *, pool):
            raise RuntimeError("quality exploded")

        monkeypatch.setattr(shot_pipeline, "detect_quality", boom)
        report = ShotAnalysisPipeline().run(_request(source, tmp_path / "out"))
        assert report.status == "partial"
        assert _step(report, "detect_quality").status == "failed"
        assert _step(report, "validate").status == "complete"
        assert any("detect_quality" in w for w in report.warnings)

    def test_validate_errors_fail_pipeline(
        self, source, tmp_path, monkeypatch, patched
    ):
        from memoloupe.validate.json_contracts import ValidationIssue

        issue = ValidationIssue(
            severity="error",
            artifact="shots",
            json_path="$",
            message="fake error",
            expected="x",
            actual="y",
        )
        monkeypatch.setattr(
            shot_pipeline, "validate_output_dir", lambda root, *, strict=False: [issue]
        )
        report = ShotAnalysisPipeline().run(_request(source, tmp_path / "out"))
        assert report.status == "failed"
        assert _step(report, "validate").status == "failed"
        # 渲染发生在校验之前（渲染后必须先校验再标完成）
        names = [s.name for s in report.steps]
        assert names.index("render_shot_html") < names.index("validate")


class TestStubBuilders:
    """stub 产物直接过 schema（docs/03 §7 降级矩阵）。"""

    shots = _fake_shots(Path("fake.mp4"), _fake_media(Path("fake.mp4"), {}), {})["shots"]
    media = _fake_media(Path("fake.mp4"), {})

    def test_asr_stub(self):
        data = build_asr_stub()
        validate_artifact(ArtifactName.ASR, data)
        assert data["status"] == "skipped"
        assert data["transcript"]["segments"] == []

    def test_music_flags_stub(self):
        data = build_music_flags_stub(self.shots, DEFAULT_CONFIG)
        validate_artifact(ArtifactName.MUSIC_FLAGS, data)
        assert data["status"] == "skipped"
        assert data["stateTally"] == {"music": 0, "silent": 0, "unknown": 1}
        assert data["shots"][0]["state"] == "unknown"
        assert data["shots"][0]["confidence"] == "unknown"

    def test_audio_cuts_stub(self):
        data = build_audio_cuts_stub(self.shots, DEFAULT_CONFIG)
        validate_artifact(ArtifactName.AUDIO_CUTS, data)
        assert data["status"] == "unavailable"
        assert data["boundaries"] == []
        entry = data["shots"][0]
        assert entry["boundaryIn"]["classification"] == "sourceStart"
        assert entry["boundaryIn"]["confidence"] == "high"
        assert entry["boundaryOut"]["classification"] == "sourceEnd"
        # 多镜头时中间边界为 unavailable
        two_shots = [
            {**self.shots[0], "shotID": "SH0001", "finalEndMs": 1000, "durationMs": 1000,
             "boundaryOut": {"type": "hardCutCandidate", "confidence": "high", "metric": None}},
            {**self.shots[0], "shotID": "SH0002", "sequenceIndex": 2,
             "finalStartMs": 1000, "finalEndMs": 2000,
             "boundaryIn": {"type": "hardCutCandidate", "confidence": "high", "metric": None}},
        ]
        data2 = build_audio_cuts_stub(two_shots, DEFAULT_CONFIG)
        validate_artifact(ArtifactName.AUDIO_CUTS, data2)
        assert data2["shots"][0]["boundaryOut"]["classification"] == "unavailable"
        assert data2["shots"][1]["boundaryIn"]["classification"] == "unavailable"
        assert data2["shots"][1]["boundaryOut"]["classification"] == "sourceEnd"

    def test_camera_motion_stub(self):
        data = build_camera_motion_stub(self.shots, self.media, DEFAULT_CONFIG)
        validate_artifact(ArtifactName.CAMERA_MOTION, data)
        assert data["analysis"]["capabilityStatus"] == "unavailable"
        assert data["analysis"]["sampleFps"] == 2.0
        assert data["analysis"]["maximumFramesPerShot"] == 12
        shot = data["shots"][0]
        assert shot["cameraMovement"] == "unknown"
        assert shot["sampleCount"] == 0
        assert shot["needsReview"] is True

    def test_unified_media_stub(self):
        clips = [
            {
                "shotID": "SH0001",
                "startMs": 0,
                "endMs": 2000,
                "durationMs": 2000,
                "file": "clips/SH0001.mp4",
                "modelFile": "clips/model-proxy/SH0001-fake.mp4",
                "modelDurationMs": 2000,
                "modelNormalization": None,
            }
        ]
        data = build_unified_media_stub(clips, self.media, DEFAULT_CONFIG)
        validate_artifact(ArtifactName.UNIFIED_MEDIA, data)
        assert data["status"] == "skipped"
        assert data["terminal"] is False
        assert data["shotStatuses"] == {"SH0001": "pending"}
        assert data["pendingShots"] == 1
        assert data["request"]["model"] == "unavailable-m1"
        assert data["request"]["externalFrameExtraction"] is False
        assert data["request"]["sourceRevisionID"] == "fakefakefake"


class TestServiceWiring:
    """服务构造与降级路径（docs/03 §7）。"""

    def test_mock_services_run_asr_and_unified_for_real(
        self, source, tmp_path, patched, monkeypatch
    ):
        from memoloupe.analysis.asr_stage import run_asr_stage as real_asr_stage

        def media_with_audio(source, config, *, pool=None, analyzed_range=None):
            doc = _fake_media(source, config, pool=pool, analyzed_range=analyzed_range)
            doc["source"]["audioTracks"] = [
                {
                    "trackID": "A1",
                    "language": "und",
                    "channels": 1,
                    "sampleRate": 44100,
                    "hasSpeech": "unknown",
                    "hasMusic": "unknown",
                    "hasEffects": "unknown",
                }
            ]
            return doc

        monkeypatch.setattr(shot_pipeline, "probe_media", media_with_audio)
        monkeypatch.setattr(shot_pipeline, "run_asr_stage", real_asr_stage)

        out_dir = tmp_path / "out"
        report = ShotAnalysisPipeline().run(
            _request(source, out_dir, mock_services=True)
        )
        assert report.status == "complete"
        unified = json.loads(
            (out_dir / "raw" / "unified-media.json").read_text(encoding="utf-8")
        )
        assert unified["status"] == "complete"
        assert unified["shotStatuses"] == {"SH0001": "succeeded"}
        asr = json.loads((out_dir / "raw" / "asr.json").read_text(encoding="utf-8"))
        assert asr["status"] == "complete"
        assert _step(report, "run_asr").status == "complete"
        assert _step(report, "unified_media_analysis").status == "complete"

    def test_config_builds_openai_asr_service(self, source, tmp_path, patched, monkeypatch):
        from memoloupe.services.asr import OpenAICompatibleASR

        captured: dict = {}

        def capture(source, media, config, service=None):
            captured["service"] = service
            return build_asr_stub()

        monkeypatch.setattr(shot_pipeline, "run_asr_stage", capture)
        config = load_config(
            {"asr": {"apiKey": "sk-x", "baseUrl": "http://127.0.0.1:9", "model": "whisper"}},
            env={},
        )
        ShotAnalysisPipeline().run(_request(source, tmp_path / "out", config=config))
        assert isinstance(captured["service"], OpenAICompatibleASR)

        # 去掉配置后 service marker 变化 → asr 步骤重跑且 service=None（skipped）
        report = ShotAnalysisPipeline().run(
            _request(source, tmp_path / "out", config=load_config(env={}))
        )
        assert captured["service"] is None
        assert _step(report, "run_asr").status == "skipped"

    def test_unified_permanent_failure_marks_partial_and_reruns(
        self, source, tmp_path, patched
    ):
        from memoloupe.services.base import TransientServiceError
        from memoloupe.services.mock import MockUnifiedMediaService

        def always_fail(clips, group, call_index):
            raise TransientServiceError("HTTP 429: slow down")

        config = load_config({"unifiedModel": {"maxRetries": 0}}, env={})
        out_dir = tmp_path / "out"
        first = ShotAnalysisPipeline().run(
            _request(
                source,
                out_dir,
                config=config,
                unified_service=MockUnifiedMediaService(always_fail),
            )
        )
        assert first.status == "partial"
        assert _step(first, "unified_media_analysis").status == "failed"
        unified = json.loads(
            (out_dir / "raw" / "unified-media.json").read_text(encoding="utf-8")
        )
        assert unified["status"] == "failed"
        assert unified["shotStatuses"] == {"SH0001": "permanent_failure"}

        # manifest 不记 complete → 第二次运行 unified 步骤自动重跑（断点续跑）
        second_mock = MockUnifiedMediaService(always_fail)
        second = ShotAnalysisPipeline().run(
            _request(source, out_dir, config=config, unified_service=second_mock)
        )
        assert second.status == "partial"
        assert second_mock.calls  # 确实重新请求了
        assert _step(second, "unified_media_analysis").status == "failed"


def _fake_shots_two(source, media, config, *, pool=None) -> dict:
    doc = _fake_shots(source, media, config, pool=pool)
    template = doc["shots"][0]
    shot1 = {
        **template,
        "shotID": "SH0001",
        "sequenceIndex": 1,
        "detectedEndMs": 1000,
        "finalEndMs": 1000,
        "durationMs": 1000,
        "boundaryOut": {"type": "hardCutCandidate", "confidence": "high", "metric": None},
    }
    shot2 = {
        **template,
        "shotID": "SH0002",
        "sequenceIndex": 2,
        "detectedStartMs": 1000,
        "finalStartMs": 1000,
        "durationMs": 1000,
        "boundaryIn": {"type": "hardCutCandidate", "confidence": "high", "metric": None},
    }
    doc["shots"] = [shot1, shot2]
    doc["boundaries"] = []
    return doc


MOVED = [
    {
        "visualTimeMs": 1000,
        "audioTimeMs": 1080,
        "offsetMs": 80,
        "audioBoundaryID": "AU0001",
        "leftShotID": "SH0001",
        "rightShotID": "SH0002",
    }
]


def _fake_audio_cuts_moved(source, shots, media, config, *, pool=None, align_boundaries=False) -> dict:
    doc = build_audio_cuts_stub(shots.get("shots", []), config)
    doc["status"] = "complete"
    # 无条件携带移动计划：pipeline 侧必须自己按 align 开关决定是否采纳
    doc["movedBoundaries"] = list(MOVED)
    return doc


class TestAlignBoundaries:
    """--align-shot-boundaries-to-audio：final 边界移动、指纹失效与幂等。"""

    def _run_two_shots(self, source, tmp_path, patched, monkeypatch, **kwargs):
        monkeypatch.setattr(shot_pipeline, "detect_shots", _fake_shots_two)
        monkeypatch.setattr(
            shot_pipeline, "detect_audio_cuts", _fake_audio_cuts_moved
        )
        return ShotAnalysisPipeline().run(_request(source, tmp_path / "out", **kwargs))

    def test_align_rewrites_final_boundaries_and_keeps_detected(
        self, source, tmp_path, patched, monkeypatch
    ):
        report = self._run_two_shots(
            source, tmp_path, patched, monkeypatch, align_boundaries=True
        )
        assert report.status == "complete"
        shots = json.loads(
            (tmp_path / "out" / "raw" / "shots.json").read_text(encoding="utf-8")
        )["shots"]
        assert shots[0]["finalEndMs"] == 1080
        assert shots[0]["durationMs"] == 1080
        assert shots[1]["finalStartMs"] == 1080
        assert shots[1]["durationMs"] == 920
        # detected 边界永不修改
        assert shots[0]["detectedEndMs"] == 1000
        assert shots[1]["detectedStartMs"] == 1000
        assert any("音频对齐" in w for w in report.warnings)

    def test_align_off_keeps_detected_boundaries(
        self, source, tmp_path, patched, monkeypatch
    ):
        report = self._run_two_shots(source, tmp_path, patched, monkeypatch)
        assert report.status == "complete"
        shots = json.loads(
            (tmp_path / "out" / "raw" / "shots.json").read_text(encoding="utf-8")
        )["shots"]
        assert shots[0]["finalEndMs"] == 1000
        assert shots[1]["finalStartMs"] == 1000

    def test_aligned_run_is_fully_reusable_on_second_run(
        self, source, tmp_path, patched, monkeypatch
    ):
        self._run_two_shots(source, tmp_path, patched, monkeypatch, align_boundaries=True)
        calls_before = dict(patched)
        second = ShotAnalysisPipeline().run(
            _request(source, tmp_path / "out", align_boundaries=True)
        )
        assert second.status == "complete"
        # 对齐后的 shots.json 直接被复用，detect_shots/audio_cuts 都不重跑
        assert patched == calls_before
        for step in second.steps:
            if step.name == "acquire_lock":
                continue
            assert step.status == "reused", step.name
        shots = json.loads(
            (tmp_path / "out" / "raw" / "shots.json").read_text(encoding="utf-8")
        )["shots"]
        assert shots[0]["finalEndMs"] == 1080  # 未二次移动

    def test_mismatched_move_plan_is_not_applied(
        self, source, tmp_path, patched, monkeypatch
    ):
        monkeypatch.setattr(shot_pipeline, "detect_shots", _fake_shots_two)

        def bad_move(source, shots, media, config, *, pool=None, align_boundaries=False):
            doc = build_audio_cuts_stub(shots.get("shots", []), config)
            doc["status"] = "complete"
            doc["movedBoundaries"] = [{**MOVED[0], "visualTimeMs": 999}]
            return doc

        monkeypatch.setattr(shot_pipeline, "detect_audio_cuts", bad_move)
        report = ShotAnalysisPipeline().run(
            _request(source, tmp_path / "out", align_boundaries=True)
        )
        assert report.status == "complete"
        shots = json.loads(
            (tmp_path / "out" / "raw" / "shots.json").read_text(encoding="utf-8")
        )["shots"]
        assert shots[0]["finalEndMs"] == 1000  # 守卫拒绝不一致的移动计划
