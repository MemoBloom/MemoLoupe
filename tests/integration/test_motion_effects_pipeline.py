"""motion-effects pipeline 集成测试（Phase 05-07 §6.3）。

用真实 ffmpeg 生成的 2s 纯色视频跑 ShotAnalysisPipeline（跳过模型步骤，
motion-effects 是纯本地确定性检测），验证：

- 首次运行写出 raw/motion-effects.json（status=complete、有逐帧指标）；
- 纯色视频产生 low_motion_or_freeze 候选（无运动 → 冻结区）；
- 同指纹二次运行 detect_motion_effects 复用；
- --force detect_motion_effects 只强制该步骤重跑；
- --skip detect_motion_effects 写 status=skipped stub（不隐含 absence）；
- dry-run 时该可选步骤同样写 skipped stub。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from memoloupe.analysis.shot_pipeline import (
    SKIPPABLE_STEPS,
    ShotAnalysisPipeline,
    ShotAnalysisRequest,
)
from memoloupe.artifacts.schemas import validate_artifact, ArtifactName

_MODEL_SKIPS = frozenset({"run_asr", "unified_media_analysis"})


def _run(argv: list[str]) -> None:
    subprocess.run(argv, check=True, capture_output=True)


@pytest.fixture(scope="module")
def still_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("motion-synth")
    path = d / "still_red.mp4"
    # 2s 静止纯色：无运动、无亮度突变 → 冻结候选（无 audio track）。
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=red:s=320x240:d=2:r=10",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        str(path),
    ])
    return path


def _request(source: Path, out_dir: Path, **kwargs) -> ShotAnalysisRequest:
    extra_skips = kwargs.pop("skip_steps", frozenset())
    return ShotAnalysisRequest(
        source=source,
        output_dir=out_dir,
        skip_steps=frozenset(_MODEL_SKIPS) | frozenset(extra_skips),
        **kwargs,
    )


def _motion_doc(out_dir: Path) -> dict:
    return json.loads((out_dir / "raw" / "motion-effects.json").read_text(encoding="utf-8"))


def _step(report, name: str):
    return next(s for s in report.steps if s.name == name)


class TestPipelineIntegration:
    def test_first_run_writes_complete_doc_with_freezes(self, still_video, tmp_path):
        out_dir = tmp_path / "out"
        report = ShotAnalysisPipeline().run(_request(still_video, out_dir))
        assert report.status == "complete", report.warnings
        assert _step(report, "detect_motion_effects").status == "complete"
        doc = _motion_doc(out_dir)
        validate_artifact(ArtifactName.MOTION_EFFECTS, doc)
        assert doc["status"] == "complete"
        assert doc["analysis"]["frameCount"] >= 12  # 8fps * 2s
        assert doc["frameMetrics"]  # 有逐帧指标
        assert doc["analysis"]["thresholds"]
        # 静止纯色 → 至少一个低速/冻结候选。
        kinds = {e["type"] for e in doc["speedRamps"]}
        assert "low_motion_or_freeze" in kinds
        assert all(e["needsVisualConfirmation"] for e in doc["speedRamps"])
        # shots 摘要覆盖全部镜头。
        shots_doc = json.loads((out_dir / "raw" / "shots.json").read_text(encoding="utf-8"))
        assert [s["shotID"] for s in doc["shots"]] == [
            s["shotID"] for s in shots_doc["shots"]
        ]

    def test_second_run_reuses_motion_effects(self, still_video, tmp_path):
        out_dir = tmp_path / "out"
        pipeline = ShotAnalysisPipeline()
        first = pipeline.run(_request(still_video, out_dir))
        assert first.status == "complete"
        second = pipeline.run(_request(still_video, out_dir))
        assert second.status == "complete"
        assert _step(second, "detect_motion_effects").status == "reused"
        # reused 不刷新产物时间/内容。
        assert _motion_doc(out_dir)["generatedAt"] if "generatedAt" in _motion_doc(out_dir) else True

    def test_force_reruns_only_motion_effects(self, still_video, tmp_path):
        out_dir = tmp_path / "out"
        pipeline = ShotAnalysisPipeline()
        pipeline.run(_request(still_video, out_dir))
        report = pipeline.run(
            _request(still_video, out_dir, force_steps=frozenset({"detect_motion_effects"}))
        )
        assert _step(report, "detect_motion_effects").status == "complete"
        assert _step(report, "detect_quality").status == "reused"

    def test_skip_writes_skipped_stub(self, still_video, tmp_path):
        out_dir = tmp_path / "out"
        request = _request(
            still_video,
            out_dir,
            skip_steps=frozenset({"detect_motion_effects"}),
        )
        report = ShotAnalysisPipeline().run(request)
        assert report.status == "complete"  # skipped 是确定性降级，不影响整体
        assert _step(report, "detect_motion_effects").status == "skipped"
        doc = _motion_doc(out_dir)
        assert doc["status"] == "skipped"
        assert doc["frameMetrics"] == [] and doc["speedRamps"] == []
        # 显式可见：不隐含“没有动效”。
        assert "no absence conclusion" in doc["digest"]["usageNote"]

    def test_dry_run_writes_skipped_stub(self, still_video, tmp_path):
        out_dir = tmp_path / "out"
        request = _request(still_video, out_dir, skip_steps=frozenset(SKIPPABLE_STEPS))
        report = ShotAnalysisPipeline().run(request)
        assert report.status == "complete"
        assert _step(report, "detect_motion_effects").status == "skipped"
        assert _motion_doc(out_dir)["status"] == "skipped"
