"""media 证据模块集成测试：tmp_path 合成媒体（ffmpeg lavfi），全链路跑真实 ffmpeg。

合成媒体约束：≤3s、320x240、低帧率，控制总时长。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
from memoloupe.core.config import DEFAULT_CONFIG
from memoloupe.media.audio_energy import detect_audio_energy
from memoloupe.media.clips import build_clips
from memoloupe.media.concurrency import FFmpegPool
from memoloupe.media.frames import extract_frames
from memoloupe.media.quality import detect_quality

SHOTS_3X1S = [
    {"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 1000},
    {"shotID": "SH0002", "finalStartMs": 1000, "finalEndMs": 2000},
    {"shotID": "SH0003", "finalStartMs": 2000, "finalEndMs": 3000},
]

MEDIA_STUB = {"source": {"revisionID": "a1b2c3d4e5f6"}}


def _run(argv: list[str]) -> None:
    subprocess.run(argv, check=True, capture_output=True)


def _make_three_color(path: Path) -> None:
    # 3 段颜色硬切各 1s + 440Hz sine 音轨
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=red:s=320x240:d=1:r=10",
        "-f", "lavfi", "-i", "color=green:s=320x240:d=1:r=10",
        "-f", "lavfi", "-i", "color=blue:s=320x240:d=1:r=10",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3:sample_rate=16000",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-map", "3:a",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ])


def _make_with_audio(path: Path, audio_src: str) -> None:
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=red:s=320x240:d=2:r=10",
        "-f", "lavfi", "-i", audio_src,
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ])


def _make_black_middle(path: Path) -> None:
    # 红 1s / 黑 1s / 红 1s，无音轨
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=red:s=320x240:d=1:r=10",
        "-f", "lavfi", "-i", "color=black:s=320x240:d=1:r=10",
        "-f", "lavfi", "-i", "color=red:s=320x240:d=1:r=10",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        str(path),
    ])


def _ffprobe_json(path: Path, *entries: str) -> dict:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            *entries,
            "-of", "json", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def pool() -> FFmpegPool:
    return FFmpegPool(DEFAULT_CONFIG["ffmpeg"]["globalConcurrency"])


@pytest.fixture(scope="module")
def media_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("synth")
    _make_three_color(d / "three_color.mp4")
    _make_with_audio(d / "silent.mp4", "anullsrc=r=16000:cl=mono:d=2")
    _make_with_audio(d / "loud.mp4", "sine=frequency=440:duration=2:sample_rate=16000,volume=20dB")
    _make_with_audio(d / "noaudio.mp4", "anullsrc=r=16000:cl=mono:d=1")  # 占位，下面覆盖
    # 无音轨视频
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=red:s=320x240:d=2:r=10",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        str(d / "noaudio.mp4"),
    ])
    _make_black_middle(d / "black_mid.mp4")
    return d


def test_extract_frames_three_shots(media_dir: Path, pool: FFmpegPool, tmp_path: Path) -> None:
    source = media_dir / "three_color.mp4"
    result = extract_frames(source, SHOTS_3X1S, MEDIA_STUB, DEFAULT_CONFIG, tmp_path, pool=pool)

    validate_artifact(ArtifactName.FRAME_EVIDENCE, result)
    assert result["status"] == "complete"
    assert result["request"]["sourceRevisionID"] == "a1b2c3d4e5f6"
    assert result["request"]["width"] == 640
    assert len(result["frames"]) == 3
    assert result["failedFrames"] == []
    for frame, shot in zip(result["frames"], SHOTS_3X1S):
        assert frame["shotID"] == shot["shotID"]
        assert shot["finalStartMs"] <= frame["timeMs"] < shot["finalEndMs"]
        assert frame["range"] == {"startMs": frame["timeMs"], "endMs": frame["timeMs"]}
        # fileRef 指向真实存在的文件
        assert (tmp_path / frame["fileRef"]).is_file()
        assert (tmp_path / frame["fileRef"]).stat().st_size > 0


def test_extract_frames_failure_goes_to_failed_frames(
    media_dir: Path, pool: FFmpegPool, tmp_path: Path
) -> None:
    source = media_dir / "three_color.mp4"
    # 区间超出媒体时长：抽帧必然失败
    shots = [{"shotID": "SH0001", "finalStartMs": 90000, "finalEndMs": 91000}]
    result = extract_frames(source, shots, MEDIA_STUB, DEFAULT_CONFIG, tmp_path, pool=pool)
    validate_artifact(ArtifactName.FRAME_EVIDENCE, result)
    assert result["status"] == "failed"
    assert result["frames"] == []
    assert len(result["failedFrames"]) == 1
    assert result["failedFrames"][0]["shotID"] == "SH0001"
    assert result["failedFrames"][0]["reason"]
    # 不伪造 fileRef
    assert not list((tmp_path / "evidence" / "frames").glob("*.jpg"))


def test_build_clips_structure_and_proxy(media_dir: Path, pool: FFmpegPool, tmp_path: Path) -> None:
    source = media_dir / "three_color.mp4"
    clips = build_clips(source, SHOTS_3X1S, True, DEFAULT_CONFIG, tmp_path, pool=pool)

    assert len(clips) == 3
    for clip, shot in zip(clips, SHOTS_3X1S):
        assert clip["shotID"] == shot["shotID"]
        assert clip["startMs"] == shot["finalStartMs"]
        assert clip["endMs"] == shot["finalEndMs"]
        assert clip["durationMs"] == 1000
        evidence = tmp_path / clip["file"]
        proxy = tmp_path / clip["modelFile"]
        assert evidence.is_file() and proxy.is_file()

        # 证据 clip 时长 1000±100ms
        info = _ffprobe_json(evidence, "-show_entries", "format=duration")
        duration_ms = float(info["format"]["duration"]) * 1000
        assert abs(duration_ms - 1000) <= 100

        # 短镜头（<2s，clips.v4）模型代理：镜头中点单帧 JPEG，宽 720
        assert proxy.suffix == ".jpg"
        pinfo = _ffprobe_json(
            proxy,
            "-select_streams", "v:0",
            "-show_entries", "stream=width",
        )
        assert pinfo["streams"][0]["width"] == 720

        norm = clip["modelNormalization"]
        assert norm["cacheKey"]
        assert norm["file"] == clip["modelFile"]
        assert norm["strategy"] == "frame-midpoint-w720"
        # 静帧代理的 modelDurationMs 语义为模型输入所代表的镜头时长
        assert clip["modelDurationMs"] == 1000


def test_build_clips_short_clip_image_proxy(media_dir: Path, pool: FFmpegPool, tmp_path: Path) -> None:
    source = media_dir / "three_color.mp4"
    shots = [{"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 600}]
    clips = build_clips(source, shots, True, DEFAULT_CONFIG, tmp_path, pool=pool)
    assert len(clips) == 1
    clip = clips[0]
    assert clip["modelNormalization"]["strategy"] == "frame-midpoint-w720"
    # 静帧代理：modelDurationMs 即镜头时长，不做补齐
    assert clip["modelDurationMs"] == 600
    proxy = tmp_path / clip["modelFile"]
    assert proxy.suffix == ".jpg"
    pinfo = _ffprobe_json(proxy, "-select_streams", "v:0", "-show_entries", "stream=width")
    assert pinfo["streams"][0]["width"] == 720


def test_build_clips_long_shot_video_proxy(media_dir: Path, pool: FFmpegPool, tmp_path: Path) -> None:
    source = media_dir / "three_color.mp4"
    shots = [{"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 2500}]
    clips = build_clips(source, shots, True, DEFAULT_CONFIG, tmp_path, pool=pool)

    assert len(clips) == 1
    clip = clips[0]
    assert clip["modelFile"].endswith(".mp4")
    norm = clip["modelNormalization"]
    assert norm["cacheKey"]
    assert norm["file"] == clip["modelFile"]
    assert norm["strategy"] == "reencode-w720-fps10"
    assert "padded" not in norm

    # 视频代理：宽 720、fps 10、实测时长约 2500ms
    proxy = tmp_path / clip["modelFile"]
    pinfo = _ffprobe_json(
        proxy,
        "-select_streams", "v:0",
        "-show_entries", "stream=width,avg_frame_rate:format=duration",
    )
    stream = pinfo["streams"][0]
    assert stream["width"] == 720
    num, den = stream["avg_frame_rate"].split("/")
    assert float(num) / float(den) == pytest.approx(10.0)
    assert clip["modelDurationMs"] >= 2400


def test_audio_energy_labels(media_dir: Path, pool: FFmpegPool) -> None:
    shots = [
        {"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 2000},
    ]
    silent = detect_audio_energy(media_dir / "silent.mp4", shots, True, DEFAULT_CONFIG, pool=pool)
    validate_artifact(ArtifactName.AUDIO_ENERGY, silent)
    assert silent["hasAudio"] is True
    assert silent["shots"][0]["label"] == "静音"
    assert silent["shots"][0]["frameCount"] > 0

    loud = detect_audio_energy(media_dir / "loud.mp4", shots, True, DEFAULT_CONFIG, pool=pool)
    validate_artifact(ArtifactName.AUDIO_ENERGY, loud)
    assert loud["shots"][0]["label"] in ("高", "峰值")
    assert loud["shots"][0]["medianDb"] > -25.0


def test_audio_energy_no_audio_track(media_dir: Path, pool: FFmpegPool) -> None:
    shots = [
        {"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 2000},
    ]
    result = detect_audio_energy(
        media_dir / "noaudio.mp4", shots, False, DEFAULT_CONFIG, pool=pool
    )
    validate_artifact(ArtifactName.AUDIO_ENERGY, result)
    assert result["hasAudio"] is False
    entry = result["shots"][0]
    assert entry["label"] == "unknown"
    assert entry["medianDb"] is None
    assert entry["frameCount"] == 0
    assert "minDb" not in entry and "maxDb" not in entry


def test_quality_black_middle_shot(media_dir: Path, pool: FFmpegPool) -> None:
    result = detect_quality(
        media_dir / "black_mid.mp4", SHOTS_3X1S, False, DEFAULT_CONFIG, pool=pool
    )
    validate_artifact(ArtifactName.QUALITY_FLAGS, result)
    assert result["status"] == "complete"
    # 无音轨：absent，任何镜头都不得报音频削波
    assert result["audioStatus"] == "absent"
    by_id = {s["shotID"]: s for s in result["shots"]}
    assert "黑场" in by_id["SH0002"]["flags"]
    assert "黑场" not in by_id["SH0001"]["flags"]
    assert "黑场" not in by_id["SH0003"]["flags"]
    for shot in result["shots"]:
        assert "音频削波" not in shot["flags"]
        assert shot["confidence"] == "high"
