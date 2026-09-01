"""audio_cuts + audio_music 集成测试：ffmpeg 合成媒体，真实解码与检测。"""

from __future__ import annotations

import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
from memoloupe.core.config import DEFAULT_CONFIG
from memoloupe.media.audio_cuts import detect_audio_cuts
from memoloupe.media.audio_music import detect_music
from memoloupe.media.probe import probe_media
from memoloupe.media.shots import detect_shots

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe 不在 PATH",
)


def _run_ffmpeg(argv: list[str]) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-y", *argv],
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")


@pytest.fixture(scope="module")
def sync_cut_clip(tmp_path_factory) -> Path:
    """0-2s 红+440Hz，2-4s 蓝+880Hz：画面硬切与音色突变都在 2s。"""
    out = tmp_path_factory.mktemp("media") / "sync-cut.mp4"
    _run_ffmpeg(
        [
            "-f", "lavfi", "-i", "color=red:size=320x240:rate=30:duration=2",
            "-f", "lavfi", "-i", "color=blue:size=320x240:rate=30:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100:duration=2",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1[v];[2:a][3:a]concat=n=2:v=0:a=1[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "mpeg4", "-c:a", "aac",
            str(out),
        ]
    )
    return out


@pytest.fixture(scope="module")
def continuous_audio_clip(tmp_path_factory) -> Path:
    """画面 2s 处红蓝硬切，音频为连续 440Hz。"""
    out = tmp_path_factory.mktemp("media") / "continuous.mp4"
    _run_ffmpeg(
        [
            "-f", "lavfi", "-i", "color=red:size=320x240:rate=30:duration=2",
            "-f", "lavfi", "-i", "color=blue:size=320x240:rate=30:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=4",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]",
            "-map", "[v]", "-map", "2:a",
            "-c:v", "mpeg4", "-c:a", "aac",
            str(out),
        ]
    )
    return out


@pytest.fixture(scope="module")
def no_audio_clip(tmp_path_factory) -> Path:
    """红蓝硬切视频，无音轨。"""
    out = tmp_path_factory.mktemp("media") / "no-audio.mp4"
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


@pytest.fixture(scope="module")
def speech_then_music_clip(tmp_path_factory) -> Path:
    """0-2s 低幅 pink noise（语音状），2-4s 响亮双正弦（音乐状）。"""
    out = tmp_path_factory.mktemp("media") / "speech-music.mp4"
    _run_ffmpeg(
        [
            "-f", "lavfi", "-i", "color=gray:size=320x240:rate=30:duration=4",
            "-f", "lavfi", "-i",
            "anoisesrc=color=pink:amplitude=0.02:sample_rate=44100:duration=2",
            "-f", "lavfi", "-i",
            "aevalsrc=0.35*sin(2*PI*220*t)+0.35*sin(2*PI*440*t):sample_rate=44100:duration=2",
            "-filter_complex", "[1:a][2:a]concat=n=2:v=0:a=1[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "mpeg4", "-c:a", "aac",
            str(out),
        ]
    )
    return out


@pytest.fixture(scope="module")
def silent_clip(tmp_path_factory) -> Path:
    """4s 视频 + 全静音音轨。"""
    out = tmp_path_factory.mktemp("media") / "silent.mp4"
    _run_ffmpeg(
        [
            "-f", "lavfi", "-i", "color=gray:size=320x240:rate=30:duration=4",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=44100:duration=4",
            "-c:v", "mpeg4", "-c:a", "aac", "-shortest",
            str(out),
        ]
    )
    return out


@pytest.fixture(scope="module")
def continuous_music_clip(tmp_path_factory) -> Path:
    """4s 连续双音伴奏；ASR 可同时把整段识别为演唱人声。"""
    out = tmp_path_factory.mktemp("media") / "continuous-music.mp4"
    _run_ffmpeg(
        [
            "-f", "lavfi", "-i", "color=gray:size=320x240:rate=30:duration=4",
            "-f", "lavfi", "-i",
            "aevalsrc=0.30*sin(2*PI*220*t)+0.20*sin(2*PI*440*t):sample_rate=44100:duration=4",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "mpeg4", "-c:a", "aac", "-shortest",
            str(out),
        ]
    )
    return out


@pytest.fixture(scope="module")
def loud_noise_clip(tmp_path_factory) -> Path:
    """4s 响亮宽带噪声，防止全轨扫描把响度直接等同于音乐。"""
    out = tmp_path_factory.mktemp("media") / "loud-noise.mp4"
    _run_ffmpeg(
        [
            "-f", "lavfi", "-i", "color=gray:size=320x240:rate=30:duration=4",
            "-f", "lavfi", "-i",
            "anoisesrc=color=white:amplitude=0.30:sample_rate=44100:duration=4",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "mpeg4", "-c:a", "aac", "-shortest",
            str(out),
        ]
    )
    return out


def _two_shots() -> list[dict]:
    return [
        {"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 2000},
        {"shotID": "SH0002", "finalStartMs": 2000, "finalEndMs": 4000},
    ]


def _asr_with_speech(end_ms: int = 1900) -> dict:
    return {
        "service": "asr",
        "status": "complete",
        "transcript": {
            "segments": [{"startMs": 100, "endMs": end_ms, "text": "第一段解说。"}]
        },
    }


class TestAudioCutsIntegration:
    def test_synchronized_cut(self, sync_cut_clip) -> None:
        media = probe_media(sync_cut_clip, DEFAULT_CONFIG)
        shots = detect_shots(sync_cut_clip, media, DEFAULT_CONFIG)
        result = detect_audio_cuts(sync_cut_clip, shots, media, DEFAULT_CONFIG)
        validate_artifact(ArtifactName.AUDIO_CUTS, result)

        assert result["status"] == "complete"
        assert result["analysis"]["selectedBoundaryCount"] == len(result["boundaries"])
        assert len(result["boundaries"]) >= 1

        # 2s 处的视觉硬切应被分类为音画同步切
        out_side = result["shots"][0]["boundaryOut"]
        assert out_side["classification"] == "synchronizedCut"
        assert out_side["offsetMs"] == out_side["audioTimeMs"] - out_side["visualTimeMs"]
        assert abs(out_side["offsetMs"]) <= result["analysis"]["syncToleranceMs"]
        assert abs(out_side["visualTimeMs"] - 2000) <= 600
        # audioBoundaryID 引用闭合
        ids = {b["audioBoundaryID"] for b in result["boundaries"]}
        assert out_side["audioBoundaryID"] in ids
        # 对应的 in 侧共享同一分类
        assert result["shots"][1]["boundaryIn"] == out_side
        # 首尾
        assert result["shots"][0]["boundaryIn"]["classification"] == "sourceStart"
        assert result["shots"][1]["boundaryOut"]["classification"] == "sourceEnd"

    def test_align_boundaries_respects_min_shot_length(self, sync_cut_clip) -> None:
        # 2s 镜头短于 minimumFrames/analysisFps = 4s，即使高置信同步切也不移动
        media = probe_media(sync_cut_clip, DEFAULT_CONFIG)
        shots = detect_shots(sync_cut_clip, media, DEFAULT_CONFIG)
        result = detect_audio_cuts(
            sync_cut_clip, shots, media, DEFAULT_CONFIG, align_boundaries=True
        )
        validate_artifact(ArtifactName.AUDIO_CUTS, result)
        assert result["movedBoundaries"] == []

    def test_picture_cut_audio_continuous(self, continuous_audio_clip) -> None:
        media = probe_media(continuous_audio_clip, DEFAULT_CONFIG)
        shots = detect_shots(continuous_audio_clip, media, DEFAULT_CONFIG)
        result = detect_audio_cuts(continuous_audio_clip, shots, media, DEFAULT_CONFIG)
        validate_artifact(ArtifactName.AUDIO_CUTS, result)
        assert result["status"] == "complete"
        out_side = result["shots"][0]["boundaryOut"]
        assert out_side["classification"] == "pictureCutAudioContinuous"

    def test_no_audio_track(self, no_audio_clip) -> None:
        media = probe_media(no_audio_clip, DEFAULT_CONFIG)
        shots = detect_shots(no_audio_clip, media, DEFAULT_CONFIG)
        result = detect_audio_cuts(no_audio_clip, shots, media, DEFAULT_CONFIG)
        validate_artifact(ArtifactName.AUDIO_CUTS, result)
        assert result["status"] == "unavailable"
        assert result["boundaries"] == []
        assert result["shots"][0]["boundaryIn"]["classification"] == "sourceStart"
        assert result["shots"][0]["boundaryOut"]["classification"] == "unavailable"
        assert result["shots"][1]["boundaryOut"]["classification"] == "sourceEnd"


class TestDetectMusicIntegration:
    def test_continuous_music_survives_full_asr_coverage(
        self, continuous_music_clip
    ) -> None:
        media = probe_media(continuous_music_clip, DEFAULT_CONFIG)
        asr = _asr_with_speech(end_ms=3900)
        result = detect_music(
            continuous_music_clip, _two_shots(), asr, media, DEFAULT_CONFIG
        )
        validate_artifact(ArtifactName.MUSIC_FLAGS, result)

        assert result["speechGaps"] == []
        assert [shot["state"] for shot in result["shots"]] == ["music", "music"]
        assert all(shot["musicOverlapRatio"] >= 0.9 for shot in result["shots"])
        assert all(
            interval["origin"] == "fullRangeTexture+gapAnchor"
            for interval in result["musicIntervals"]
        )

    def test_loud_noise_is_not_music_with_full_asr_coverage(
        self, loud_noise_clip
    ) -> None:
        media = probe_media(loud_noise_clip, DEFAULT_CONFIG)
        asr = _asr_with_speech(end_ms=3900)
        result = detect_music(
            loud_noise_clip, _two_shots(), asr, media, DEFAULT_CONFIG
        )
        validate_artifact(ArtifactName.MUSIC_FLAGS, result)

        assert all(shot["state"] != "music" for shot in result["shots"])

    def test_music_after_speech(self, speech_then_music_clip) -> None:
        media = probe_media(speech_then_music_clip, DEFAULT_CONFIG)
        result = detect_music(
            speech_then_music_clip, _two_shots(), _asr_with_speech(), media, DEFAULT_CONFIG
        )
        validate_artifact(ArtifactName.MUSIC_FLAGS, result)
        assert result["status"] == "complete"
        second = result["shots"][1]
        assert second["state"] == "music"
        assert second["confidence"] == "high"
        assert second["musicOverlapRatio"] >= 0.9
        # stateTally 与 shots 聚合一致
        tally = Counter(s["state"] for s in result["shots"])
        assert result["stateTally"] == {
            "music": tally["music"],
            "silent": tally["silent"],
            "unknown": tally["unknown"],
        }
        # ASR 间隙作为锚点，但不再关闭全轨扫描。
        assert result["speechGaps"]
        assert any(g["state"] == "music" for g in result["speechGaps"])
        assert all(
            i["origin"] == "fullRangeTexture+gapAnchor"
            for i in result["musicIntervals"]
        )

    def test_degraded_without_asr(self, speech_then_music_clip) -> None:
        media = probe_media(speech_then_music_clip, DEFAULT_CONFIG)
        asr = {"service": "asr", "status": "failed", "transcript": {"segments": []}}
        result = detect_music(
            speech_then_music_clip, _two_shots(), asr, media, DEFAULT_CONFIG
        )
        validate_artifact(ArtifactName.MUSIC_FLAGS, result)
        assert result["speechGaps"] == []
        second = result["shots"][1]
        assert second["state"] == "music"
        assert second["confidence"] == "medium"  # 降一档
        assert "ASR 不可用" in second["basis"]

    def test_all_silent(self, silent_clip) -> None:
        media = probe_media(silent_clip, DEFAULT_CONFIG)
        asr = {"service": "asr", "status": "complete", "transcript": {"segments": []}}
        result = detect_music(silent_clip, _two_shots(), asr, media, DEFAULT_CONFIG)
        validate_artifact(ArtifactName.MUSIC_FLAGS, result)
        assert result["status"] == "complete"
        assert [s["state"] for s in result["shots"]] == ["silent", "silent"]
        assert result["stateTally"] == {"music": 0, "silent": 2, "unknown": 0}
        assert result["musicIntervals"] == []

    def test_no_audio_track(self, no_audio_clip) -> None:
        media = probe_media(no_audio_clip, DEFAULT_CONFIG)
        result = detect_music(no_audio_clip, _two_shots(), None, media, DEFAULT_CONFIG)
        validate_artifact(ArtifactName.MUSIC_FLAGS, result)
        assert result["status"] == "unavailable"
        assert result["stateTally"] == {"music": 0, "silent": 0, "unknown": 2}
