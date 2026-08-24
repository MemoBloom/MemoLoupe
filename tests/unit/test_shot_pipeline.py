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
            "extract_frames",
            "build_clips",
            "detect_audio_energy",
            "detect_quality",
            "stub_unavailable",
            "render_shot_html",
            "validate",
        ]
        assert _step(report, "acquire_lock").status == "complete"
        assert all(
            _step(report, n).status == "complete"
            for n in names
            if n not in ("acquire_lock", "stub_unavailable")
        )
        assert _step(report, "stub_unavailable").status == "unavailable"
        # 所有步骤计时非负
        assert all(s.elapsed_ms >= 0 for s in report.steps)
        # 产物落盘（raw/*.json + HTML + manifest）
        for artifact in (
            "media", "shots", "frame-evidence", "audio-energy", "quality-flags",
            "asr", "music-flags", "audio-cuts", "camera-motion", "unified-media",
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
        assert _step(report, "extract_frames").status == "complete"
        assert _step(report, "stub_unavailable").status == "unavailable"

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
        assert _step(report, "stub_unavailable").status == "reused"
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
