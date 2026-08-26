"""Phase 1 CLI 生产调试能力 e2e（roadmap 05-04）。

覆盖：--skip 显式降级（跳过 ≠ absent）、--dry-run 不调外部服务、
--render-only 不改 raw、--strict 门禁、--max-shots 调试裁剪。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from memoloupe.cli.main import EXIT_OK, EXIT_STAGE_FAILED, main

from conftest import E2E_SHOTS_ENV, synthesize_hardcut_video  # noqa: F401

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe 不在 PATH",
)


def _read(out_dir: Path, name: str) -> dict:
    return json.loads((out_dir / "raw" / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _shot_env(monkeypatch):
    """与 conftest.shot_env 相同：细粒度检测参数。"""
    for key, value in E2E_SHOTS_ENV.items():
        monkeypatch.setenv(key, value)


class TestSkipSteps:
    def test_skip_run_asr_writes_skipped_artifact(self, hardcut_video, tmp_path):
        out = tmp_path / "out"
        code = main(
            [
                "shot", str(hardcut_video), "--output-dir", str(out),
                "--skip", "run_asr", "--skip", "unified_media_analysis",
                "--skip", "analyze_camera_motion",
            ]
        )
        assert code == EXIT_OK
        # 跳过 ≠ absent：产物存在且显式 skipped。
        assert _read(out, "asr")["status"] == "skipped"
        assert _read(out, "unified-media")["status"] == "skipped"
        camera = _read(out, "camera-motion")
        assert camera["analysis"]["capabilityStatus"] == "unavailable"
        # 未跳过的确定性步骤正常产出。
        assert _read(out, "shots")["shots"]

    def test_skip_invalid_step_is_usage_error(self, hardcut_video, tmp_path):
        code = main(
            ["shot", str(hardcut_video), "--output-dir", str(tmp_path / "out"),
             "--skip", "detect_shots"]
        )
        assert code == 2

    def test_skip_frames_writes_failed_stub(self, hardcut_video, tmp_path):
        out = tmp_path / "out"
        code = main(
            ["shot", str(hardcut_video), "--output-dir", str(out),
             "--skip", "extract_frames"]
        )
        assert code == EXIT_OK
        frames = _read(out, "frame-evidence")
        assert frames["status"] == "failed"
        assert frames["frames"] == []
        assert frames["failedFrames"]


class TestDryRun:
    def test_dry_run_skips_all_optional_steps(self, hardcut_video, tmp_path):
        out = tmp_path / "out"
        code = main(
            ["shot", str(hardcut_video), "--output-dir", str(out), "--dry-run"]
        )
        assert code == EXIT_OK
        # 基础产物存在：切镜/clip。
        assert _read(out, "shots")["shots"]
        assert (out / "clips").is_dir()
        # 全部可选步骤显式降级。
        assert _read(out, "asr")["status"] == "skipped"
        assert _read(out, "unified-media")["status"] == "skipped"
        assert _read(out, "audio-cuts")["status"] == "unavailable"
        assert _read(out, "music-flags")["status"] == "skipped"
        assert _read(out, "audio-energy")["hasAudio"] is False
        assert _read(out, "quality-flags")["shots"][0]["confidence"] == "unknown"
        assert (
            _read(out, "camera-motion")["analysis"]["capabilityStatus"]
            == "unavailable"
        )


class TestRenderOnly:
    def test_render_only_rerenders_without_touching_raw(
        self, hardcut_video, tmp_path
    ):
        out = tmp_path / "out"
        assert main(
            ["shot", str(hardcut_video), "--output-dir", str(out), "--dry-run"]
        ) == EXIT_OK
        raw_before = {
            name: (out / "raw" / f"{name}.json").read_bytes()
            for name in ("shots", "asr", "unified-media")
        }
        html_before = (out / "shot-analysis.html").read_text(encoding="utf-8")
        assert main(
            ["shot", str(hardcut_video), "--output-dir", str(out), "--render-only"]
        ) == EXIT_OK
        for name, content in raw_before.items():
            assert (out / "raw" / f"{name}.json").read_bytes() == content
        html_after = (out / "shot-analysis.html").read_text(encoding="utf-8")
        assert html_after == html_before  # 无 raw 变化 → 渲染结果稳定


class TestStrictGate:
    def test_strict_complete_returns_zero(self, hardcut_video, tmp_path):
        out = tmp_path / "out"
        code = main(
            ["shot", str(hardcut_video), "--output-dir", str(out), "--strict"]
        )
        assert code == EXIT_OK

    def test_missing_input_is_input_error(self, hardcut_video, tmp_path):
        code = main(
            ["shot", str(tmp_path / "missing.mp4"), "--output-dir", str(tmp_path / "out")]
        )
        assert code == 3  # 输入缺失

    def test_strict_failed_returns_stage_failed(self, hardcut_video, tmp_path):
        # 不可解码的"视频"导致必需步骤失败 → 退出码 5（含 --strict 路径）。
        bogus = tmp_path / "bogus.mp4"
        bogus.write_bytes(b"not a video at all")
        code = main(
            ["shot", str(bogus), "--output-dir", str(tmp_path / "out"), "--strict"]
        )
        assert code == EXIT_STAGE_FAILED


class TestMaxShots:
    def test_max_shots_truncates(self, hardcut_video, tmp_path):
        out = tmp_path / "out"
        code = main(
            ["shot", str(hardcut_video), "--output-dir", str(out), "--max-shots", "1"]
        )
        assert code == EXIT_OK
        shots = _read(out, "shots")["shots"]
        assert len(shots) == 1
