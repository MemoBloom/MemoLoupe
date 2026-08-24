"""Phase 1 e2e 公共夹具：ffmpeg lavfi 合成硬切测试视频。

视频合成统一走这里，保证每个用例的输入一致且单段时长 <= 3s。
默认配置（shots.minimumFrames=8 / analysisFps=2）下 3s 短视频不满足最小
帧数，e2e 通过 MEMOLOUPE_SHOTS__* 环境变量覆盖（见 ``shot_env`` 夹具）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe 不在 PATH",
)

#: 默认检测参数对 3s 视频过粗，e2e 用更细的分析粒度。
E2E_SHOTS_ENV = {
    "MEMOLOUPE_SHOTS__MINIMUMFRAMES": "2",
    "MEMOLOUPE_SHOTS__ANALYSISFPS": "4.0",
}


def synthesize_hardcut_video(
    path: Path,
    *,
    # 灰度检测要求相邻段亮度可分：纯绿经 mpeg4/yuv420p 后亮度与红几乎相同
    # （实测均约 0.30），会丢切点，因此默认用红/白/蓝。
    colors: tuple[str, ...] = ("red", "white", "blue"),
    segment_sec: float = 1.0,
    fps: int = 10,
    size: str = "320x240",
    with_audio: bool = True,
) -> Path:
    """合成多段纯色硬切视频（可选正弦音轨），返回输出路径。"""
    path = Path(path)
    argv = ["ffmpeg", "-hide_banner", "-v", "error", "-y"]
    for color in colors:
        argv += [
            "-f", "lavfi",
            "-i", f"color={color}:size={size}:rate={fps}:duration={segment_sec}",
        ]
    if with_audio:
        argv += [
            "-f", "lavfi",
            "-i", "sine=frequency=440:sample_rate=44100:"
            f"duration={segment_sec * len(colors)}",
        ]
    concat_inputs = "".join(f"[{i}:v]" for i in range(len(colors)))
    argv += [
        "-filter_complex", f"{concat_inputs}concat=n={len(colors)}:v=1[v]",
        "-map", "[v]",
    ]
    if with_audio:
        argv += ["-map", f"{len(colors)}:a", "-c:a", "aac", "-shortest"]
    argv += ["-c:v", "mpeg4", str(path)]
    proc = subprocess.run(argv, capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    return path


@pytest.fixture(scope="module")
def hardcut_video(tmp_path_factory) -> Path:
    """红/白/蓝各 1s 的 3 段硬切视频（320x240@10fps + 440Hz 正弦音轨）。"""
    out = tmp_path_factory.mktemp("e2e-media") / "rgb-hardcut.mp4"
    return synthesize_hardcut_video(out)


@pytest.fixture
def shot_env(monkeypatch):
    """设置 e2e 镜头检测环境变量，返回 dict 便于局部再修改。"""
    for key, value in E2E_SHOTS_ENV.items():
        monkeypatch.setenv(key, value)
    return dict(E2E_SHOTS_ENV)
