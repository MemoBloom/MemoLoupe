"""probe + shots 集成测试：用 ffmpeg 合成媒体，跑真实探测与硬切检测。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
from memoloupe.core.config import DEFAULT_CONFIG
from memoloupe.core.time_ranges import is_contiguous
from memoloupe.media.probe import probe_media
from memoloupe.media.shots import SHOT_DETECTION_VERSION, detect_shots

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe 不在 PATH",
)

ANALYSIS_FPS = DEFAULT_CONFIG["shots"]["analysisFps"]


def _run_ffmpeg(argv: list[str]) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-y", *argv],
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")


@pytest.fixture
def av_clip(tmp_path) -> Path:
    """2s testsrc2 视频 + 2s sine 音频。"""
    out = tmp_path / "av-clip.mp4"
    _run_ffmpeg(
        [
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2",
            "-c:v", "mpeg4", "-c:a", "aac", "-shortest",
            str(out),
        ]
    )
    return out


@pytest.fixture
def hardcut_clip(tmp_path) -> Path:
    """0-2s 红、2-4s 蓝两段纯色硬切视频（无音轨）。"""
    out = tmp_path / "red-blue.mp4"
    _run_ffmpeg(
        [
            "-f", "lavfi", "-i", "color=red:size=320x240:rate=30:duration=2",
            "-f", "lavfi", "-i", "color=blue:size=320x240:rate=30:duration=2",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]",
            "-map", "[v]", "-c:v", "mpeg4",
            str(out),
        ]
    )
    return out


@pytest.fixture
def short_clip(tmp_path) -> Path:
    """0.5s 极短视频（帧数少于 minimumFrames）。"""
    out = tmp_path / "short.mp4"
    _run_ffmpeg(
        [
            "-f", "lavfi", "-i", "color=green:size=320x240:rate=30:duration=0.5",
            "-c:v", "mpeg4",
            str(out),
        ]
    )
    return out


class TestProbeMediaIntegration:
    def test_probe_real_clip(self, av_clip):
        result = probe_media(av_clip, DEFAULT_CONFIG)
        validate_artifact(ArtifactName.MEDIA, result)
        src = result["source"]
        assert src["assetID"] == "av-clip"
        assert 1900 <= src["durationMs"] <= 2100
        assert src["frameRate"] is not None and src["frameRate"] > 0
        assert src["resolution"] == {"width": 320, "height": 240}
        assert len(src["audioTracks"]) == 1
        assert src["audioTracks"][0]["channels"] >= 1
        assert src["analyzedRange"] == {"startMs": 0, "endMs": src["durationMs"]}
        assert src["analysisCoverage"][0]["status"] == "complete"


class TestDetectShotsIntegration:
    def test_hardcut_boundary_found(self, hardcut_clip):
        media = probe_media(hardcut_clip, DEFAULT_CONFIG)
        result = detect_shots(hardcut_clip, media, DEFAULT_CONFIG)
        validate_artifact(ArtifactName.SHOTS, result)

        analysis = result["analysis"]
        assert analysis["method"] == "memoClipHardCutCandidateCuts"
        assert analysis["fps"] == pytest.approx(ANALYSIS_FPS)
        assert analysis["algorithmVersion"] == SHOT_DETECTION_VERSION
        assert analysis["limitations"]

        # 唯一主要边界：红→蓝硬切，时间 2000ms ± 1000/analysisFps
        boundaries = result["boundaries"]
        assert len(boundaries) == 1
        boundary = boundaries[0]
        tolerance_sec = 1.0 / ANALYSIS_FPS
        assert boundary["timeSec"] == pytest.approx(2.0, abs=tolerance_sec)
        assert boundary["type"] == "hardCutCandidate"
        assert boundary["selectionReason"] in ("rawNegativeScore", "adaptiveOutlier")
        assert analysis["selectedBoundaryCount"] == 1

        # shots 连续无重叠、覆盖全片，detected == final
        shots = result["shots"]
        assert len(shots) == 2
        assert [s["shotID"] for s in shots] == ["SH0001", "SH0002"]
        assert [s["sequenceIndex"] for s in shots] == [1, 2]
        ranges = [(s["finalStartMs"], s["finalEndMs"]) for s in shots]
        assert is_contiguous(ranges)
        assert ranges[0][0] == 0
        assert ranges[-1][1] == media["source"]["durationMs"]
        for s in shots:
            assert s["detectedStartMs"] == s["finalStartMs"]
            assert s["detectedEndMs"] == s["finalEndMs"]
            assert s["durationMs"] == s["finalEndMs"] - s["finalStartMs"]
        assert shots[0]["boundaryIn"] == {
            "type": "sourceStart",
            "confidence": "high",
            "metric": None,
        }
        assert shots[0]["boundaryOut"]["type"] == "hardCutCandidate"
        assert shots[0]["boundaryOut"]["metric"]["timeSec"] == pytest.approx(
            boundary["timeSec"]
        )
        assert shots[1]["boundaryOut"]["type"] == "sourceEnd"

    def test_short_video_single_shot(self, short_clip):
        media = probe_media(short_clip, DEFAULT_CONFIG)
        result = detect_shots(short_clip, media, DEFAULT_CONFIG)
        validate_artifact(ArtifactName.SHOTS, result)
        assert result["boundaries"] == []
        assert result["analysis"]["selectedBoundaryCount"] == 0
        shots = result["shots"]
        assert len(shots) == 1
        assert shots[0]["shotID"] == "SH0001"
        assert shots[0]["finalStartMs"] == 0
        assert shots[0]["finalEndMs"] == media["source"]["durationMs"]
        # 镜头短于 minimumFrames/analysisFps → 需要人工复核
        assert shots[0]["needsReview"] is True

    def test_analyzed_range_restricts_detection(self, hardcut_clip):
        # 只分析 [0, 1500ms)，硬切点在 2000ms，不应检出边界
        media = probe_media(hardcut_clip, DEFAULT_CONFIG, analyzed_range=(0, 1500))
        result = detect_shots(hardcut_clip, media, DEFAULT_CONFIG)
        validate_artifact(ArtifactName.SHOTS, result)
        assert result["boundaries"] == []
        assert len(result["shots"]) == 1
        assert result["shots"][0]["finalEndMs"] == 1500
