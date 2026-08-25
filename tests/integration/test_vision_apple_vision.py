"""vision.apple_vision 集成测试：降级路径与真实 helper（docs/03 §2.11/§7）。

真实 helper 用例（标 ``real_helper``）需要 macOS + swiftc + ffmpeg，
用确定性合成视频（numpy 生成的方块纹理，crop 窗口平移/静止）验证
编译、执行、协议与聚合链路；环境不满足时 skip。
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path

import pytest

from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
from memoloupe.vision import apple_vision
from memoloupe.vision.apple_vision import analyze_camera_motion
from memoloupe.vision.unavailable import build_unavailable_camera_motion

MEDIA = {
    "source": {
        "durationMs": 4000,
        "revisionID": "a1b2c3d4e5f6",
        "resolution": {"width": 640, "height": 360},
    }
}
SHOTS = [
    {
        "shotID": "SH0001",
        "sequenceIndex": 1,
        "finalStartMs": 0,
        "finalEndMs": 4000,
        "durationMs": 4000,
    }
]


def _assert_unavailable(result: dict, reason_part: str) -> None:
    validate_artifact(ArtifactName.CAMERA_MOTION, result)
    assert result["analysis"]["capabilityStatus"] == "unavailable"
    assert reason_part in result["analysis"]["note"]
    for shot in result["shots"]:
        assert shot["cameraMovement"] == "unknown"
        assert shot["confidence"] == "unknown"
        assert shot["sampleCount"] == 0
        assert shot["needsReview"] is True


# ---------------------------------------------------------------------------
# 降级路径（不依赖真实 helper）
# ---------------------------------------------------------------------------


def test_unavailable_builder_passes_schema() -> None:
    result = build_unavailable_camera_motion(SHOTS, MEDIA, {}, "测试降级")
    _assert_unavailable(result, "测试降级")


def test_unavailable_when_helper_source_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        apple_vision, "HELPER_SOURCE", tmp_path / "no-such-main.swift"
    )
    if platform.system() == "Darwin" and shutil.which("swiftc"):
        result = analyze_camera_motion(Path("/tmp/x.mp4"), SHOTS, MEDIA, {})
        _assert_unavailable(result, "helper 源码缺失")
    else:
        # 非 macOS/无 swiftc 时更早降级，同样合法
        result = analyze_camera_motion(Path("/tmp/x.mp4"), SHOTS, MEDIA, {})
        _assert_unavailable(result, "")


def test_unavailable_when_not_macos(monkeypatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    result = analyze_camera_motion(Path("/tmp/x.mp4"), SHOTS, MEDIA, {})
    _assert_unavailable(result, "非 macOS")


def test_unavailable_when_swiftc_missing(monkeypatch) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    result = analyze_camera_motion(Path("/tmp/x.mp4"), SHOTS, MEDIA, {})
    _assert_unavailable(result, "swiftc 不可用")


def _fake_executable(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-helper.sh"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def test_unavailable_when_helper_crashes(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/swiftc")
    fake = _fake_executable(tmp_path, "echo boom >&2\nexit 1")
    monkeypatch.setattr(apple_vision, "_helper_binary", lambda swiftc, src: fake)
    result = analyze_camera_motion(Path("/tmp/x.mp4"), SHOTS, MEDIA, {})
    _assert_unavailable(result, "运行失败")


def test_unavailable_when_helper_returns_invalid_json(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/swiftc")
    fake = _fake_executable(tmp_path, "cat >/dev/null; echo 'not json'")
    monkeypatch.setattr(apple_vision, "_helper_binary", lambda swiftc, src: fake)
    result = analyze_camera_motion(Path("/tmp/x.mp4"), SHOTS, MEDIA, {})
    _assert_unavailable(result, "响应非法")


# ---------------------------------------------------------------------------
# 真实 helper：编译 + 合成视频端到端
# ---------------------------------------------------------------------------

pytestmark_real = pytest.mark.skipif(
    platform.system() != "Darwin"
    or shutil.which("swiftc") is None
    or shutil.which("ffmpeg") is None,
    reason="需要 macOS + swiftc + ffmpeg",
)


def _make_texture_png(path: Path) -> None:
    """确定性方块纹理（强角点，适合 homographic registration）。"""
    import numpy as np

    rng = np.random.default_rng(7)
    img = np.full((540, 960, 3), 32, np.uint8)
    for _ in range(120):
        w = int(rng.integers(20, 120))
        h = int(rng.integers(20, 120))
        x = int(rng.integers(0, 960 - w))
        y = int(rng.integers(0, 540 - h))
        img[y : y + h, x : x + w] = rng.integers(0, 255, 3)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "960x540",
            "-i", "-", str(path),
        ],
        input=img.tobytes(),
        check=True,
    )


def _render_video(png: Path, vf: str, out: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-loop", "1", "-i", str(png), "-vf", vf,
            "-t", "4", "-pix_fmt", "yuv420p", str(out),
        ],
        check=True,
    )


@pytest.fixture(scope="module")
def synthetic_videos(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    if platform.system() != "Darwin" or not shutil.which("ffmpeg"):
        pytest.skip("需要 macOS + ffmpeg")
    tmp = tmp_path_factory.mktemp("vision-videos")
    png = tmp / "rects.png"
    _make_texture_png(png)
    # crop 窗口以 80px/s 右移：画面内容左移 → 预期 pan_right
    pan = tmp / "pan.mp4"
    _render_video(png, "crop=640:360:x='min(t*80,320)':y=90,fps=30", pan)
    static = tmp / "static.mp4"
    _render_video(png, "fps=30", static)
    return {"pan": pan, "static": static}


@pytestmark_real
def test_real_helper_pan_video(synthetic_videos: dict[str, Path]) -> None:
    result = analyze_camera_motion(synthetic_videos["pan"], SHOTS, MEDIA, {})
    validate_artifact(ArtifactName.CAMERA_MOTION, result)
    assert result["analysis"]["capabilityStatus"] == "complete"
    assert result["analysis"]["opticalFlowEnabled"] is False
    shot = result["shots"][0]
    assert shot["sampleCount"] >= 6
    # 内容一致左移 → pan_right；Vision 对大幅逐对位移幅值有低估，方向可靠
    assert shot["cameraMovement"] == "pan_right"
    assert shot["metrics"]["medianFrameShiftX"] < 0
    assert shot["neutralMotions"] == ["horizontal_frame_shift"]


@pytestmark_real
def test_real_helper_static_video(synthetic_videos: dict[str, Path]) -> None:
    result = analyze_camera_motion(synthetic_videos["static"], SHOTS, MEDIA, {})
    validate_artifact(ArtifactName.CAMERA_MOTION, result)
    shot = result["shots"][0]
    assert shot["cameraMovement"] == "static"
    assert shot["confidence"] == "high"
    assert shot["needsReview"] is False
