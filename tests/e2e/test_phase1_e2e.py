"""Phase 1 端到端测试（docs/05）：真实 ffmpeg 媒体 + 完整 CLI/pipeline。

覆盖：全流程跑通与严格校验、缓存复用、配置失效、并发锁、中断恢复。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from memoloupe.analysis.shot_pipeline import ShotAnalysisPipeline, ShotAnalysisRequest
from memoloupe.cli.shot_analysis import (
    EXIT_OK,
    run_shot_analysis,
)
from memoloupe.validate.cross_artifact import validate_output_dir
from memoloupe.validate.html_contract import validate_html

#: Phase 1 产出的 raw JSON（story-blocks/style-profile 属于 Phase 2/3）。
PHASE1_RAW_ARTIFACTS = (
    "media",
    "shots",
    "frame-evidence",
    "audio-energy",
    "quality-flags",
    "asr",
    "music-flags",
    "audio-cuts",
    "camera-motion",
    "unified-media",
)


def _step(report, name: str):
    return next(s for s in report.steps if s.name == name)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


class TestPhase1E2E:
    def test_full_run_via_cli_and_strict_validation(
        self, hardcut_video, tmp_path, shot_env, capsys
    ):
        out_dir = tmp_path / "out"
        code = run_shot_analysis(
            [
                str(hardcut_video),
                "--output-dir", str(out_dir),
                "--start-ms", "0",
                "--end-ms", "3000",
            ]
        )
        assert code == EXIT_OK
        summary = capsys.readouterr().out
        assert "complete" in summary

        # 全部 Phase 1 产物 + HTML + manifest 落盘
        for name in PHASE1_RAW_ARTIFACTS:
            assert (out_dir / "raw" / f"{name}.json").is_file(), name
        assert (out_dir / "shot-analysis.html").is_file()
        assert (out_dir / "manifest.json").is_file()

        # 检出 3 段硬切（红/绿/蓝各 1s）
        shots = json.loads((out_dir / "raw" / "shots.json").read_text(encoding="utf-8"))
        assert len(shots["shots"]) == 3
        assert shots["analysis"]["selectedBoundaryCount"] == 2

        # 显式降级产物状态正确
        asr = json.loads((out_dir / "raw" / "asr.json").read_text(encoding="utf-8"))
        assert asr["status"] == "skipped"
        camera = json.loads(
            (out_dir / "raw" / "camera-motion.json").read_text(encoding="utf-8")
        )
        # 本机有 swiftc 时 Apple Vision 真实可用；否则显式降级
        assert camera["analysis"]["capabilityStatus"] in ("complete", "unavailable")
        unified = json.loads(
            (out_dir / "raw" / "unified-media.json").read_text(encoding="utf-8")
        )
        assert unified["status"] == "skipped"
        assert len(unified["clips"]) == 3
        # M2 起音频切点/BGM 是真实检测（有音轨 → complete）
        audio_cuts = json.loads(
            (out_dir / "raw" / "audio-cuts.json").read_text(encoding="utf-8")
        )
        assert audio_cuts["status"] == "complete"
        music = json.loads(
            (out_dir / "raw" / "music-flags.json").read_text(encoding="utf-8")
        )
        assert music["status"] == "complete"

        # 严格校验：0 错误（直接调校验器，等价于 memoloupe validate --strict）
        issues = validate_output_dir(out_dir, strict=True)
        issues += validate_html(out_dir / "shot-analysis.html", root=out_dir, strict=True)
        errors = [i for i in issues if i.severity == "error"]
        assert errors == []

    def test_rerun_with_same_config_reuses_everything(
        self, hardcut_video, tmp_path, shot_env
    ):
        out_dir = tmp_path / "out"
        pipeline = ShotAnalysisPipeline()
        first = pipeline.run(
            ShotAnalysisRequest(source=hardcut_video, output_dir=out_dir)
        )
        assert first.status == "complete"

        second = pipeline.run(
            ShotAnalysisRequest(source=hardcut_video, output_dir=out_dir)
        )
        assert second.status == "complete"
        for step in second.steps:
            if step.name == "acquire_lock":
                assert step.status == "complete"
            else:
                assert step.status == "reused", step.name

    def test_shots_config_change_invalidates_downstream(
        self, hardcut_video, tmp_path, shot_env, monkeypatch
    ):
        out_dir = tmp_path / "out"
        pipeline = ShotAnalysisPipeline()
        pipeline.run(ShotAnalysisRequest(source=hardcut_video, output_dir=out_dir))

        monkeypatch.setenv("MEMOLOUPE_SHOTS__ANALYSISFPS", "5.0")
        report = pipeline.run(
            ShotAnalysisRequest(source=hardcut_video, output_dir=out_dir)
        )
        assert report.status == "complete"
        # probe 不依赖 shots 配置 → 复用
        assert _step(report, "probe_media").status == "reused"
        # detect_shots 及镜头级下游全部重跑（run_asr 只链 media，不受影响）
        for name in (
            "detect_shots",
            "detect_audio_cuts",
            "detect_music",
            "extract_frames",
            "build_clips",
            "detect_audio_energy",
            "detect_quality",
            "unified_media_analysis",
            "analyze_camera_motion",
            "render_shot_html",
            "validate",
        ):
            assert _step(report, name).status != "reused", name
        assert _step(report, "run_asr").status == "reused"
        # 新参数下仍能检出 2 个边界
        shots = json.loads((out_dir / "raw" / "shots.json").read_text(encoding="utf-8"))
        assert len(shots["shots"]) == 3

    def test_live_lock_fails_and_preserves_outputs(
        self, hardcut_video, tmp_path, shot_env
    ):
        out_dir = tmp_path / "out"
        pipeline = ShotAnalysisPipeline()
        first = pipeline.run(
            ShotAnalysisRequest(source=hardcut_video, output_dir=out_dir)
        )
        assert first.status == "complete"
        before = _snapshot(out_dir)

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
        report = pipeline.run(
            ShotAnalysisRequest(source=hardcut_video, output_dir=out_dir)
        )
        assert report.status == "failed"
        assert _step(report, "acquire_lock").status == "failed"
        assert report.warnings
        # 除锁文件本身外，所有产物字节级未变
        after = _snapshot(out_dir)
        after.pop(".memoloupe.lock", None)
        assert after == before

    def test_interrupted_run_resumes_from_missing_artifact(
        self, hardcut_video, tmp_path, shot_env
    ):
        out_dir = tmp_path / "out"
        pipeline = ShotAnalysisPipeline()
        first = pipeline.run(
            ShotAnalysisRequest(source=hardcut_video, output_dir=out_dir)
        )
        assert first.status == "complete"

        # 模拟中断：quality-flags 及之后的产物丢失
        (out_dir / "raw" / "quality-flags.json").unlink()
        report = pipeline.run(
            ShotAnalysisRequest(source=hardcut_video, output_dir=out_dir)
        )
        assert report.status == "complete"
        # 之前步骤全部复用
        for name in (
            "probe_media",
            "detect_shots",
            "detect_audio_cuts",
            "run_asr",
            "detect_music",
            "extract_frames",
            "build_clips",
            "detect_audio_energy",
            "unified_media_analysis",
            "analyze_camera_motion",
        ):
            assert _step(report, name).status == "reused", name
        # 只补齐缺失步骤及之后的 render/validate
        assert _step(report, "detect_quality").status == "complete"
        assert _step(report, "render_shot_html").status == "complete"
        assert _step(report, "validate").status == "complete"
        assert (out_dir / "raw" / "quality-flags.json").is_file()
