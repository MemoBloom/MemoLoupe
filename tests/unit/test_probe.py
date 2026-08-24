"""media/probe 单元测试（伪造 ffprobe JSON 输出，不跑真实 ffprobe）。"""

from __future__ import annotations

import json

import pytest

from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
from memoloupe.core.config import DEFAULT_CONFIG
from memoloupe.core.errors import CapabilityUnavailableError
from memoloupe.media import probe as probe_mod
from memoloupe.media.probe import probe_media
from memoloupe.media.proc import ProcessResult


def _fake_result(payload: dict) -> ProcessResult:
    return ProcessResult(
        argv=("ffprobe",),
        returncode=0,
        stdout=json.dumps(payload).encode("utf-8"),
        stderr=b"",
        elapsed_sec=0.01,
    )


def _ffprobe_payload(
    *,
    format_duration: str | None = "61.230",
    stream_duration: str | None = None,
    avg_frame_rate: str = "30000/1001",
    width: int = 1920,
    height: int = 1080,
    rotation: int | None = None,
    audio: bool = True,
) -> dict:
    video_stream: dict = {
        "index": 0,
        "codec_type": "video",
        "width": width,
        "height": height,
        "avg_frame_rate": avg_frame_rate,
    }
    if stream_duration is not None:
        video_stream["duration"] = stream_duration
    if rotation is not None:
        video_stream["side_data_list"] = [
            {"side_data_type": "Display Matrix", "rotation": rotation}
        ]
    streams = [video_stream]
    if audio:
        streams.append(
            {
                "index": 1,
                "codec_type": "audio",
                "channels": 2,
                "sample_rate": "48000",
                "tags": {"language": "eng"},
            }
        )
    payload: dict = {"streams": streams, "format": {}}
    if format_duration is not None:
        payload["format"]["duration"] = format_duration
    return payload


@pytest.fixture
def source_file(tmp_path):
    path = tmp_path / "clip-one.mp4"
    path.write_bytes(b"fake-video-bytes")
    return path


@pytest.fixture
def fake_ffprobe(monkeypatch):
    """把 probe 模块内的 run_process 替换为返回指定 payload 的赝品。"""
    holder = {"payload": _ffprobe_payload()}

    def fake_run(argv, *, timeout_sec, stdin=None, capture_limit_bytes=None):
        return _fake_result(holder["payload"])

    monkeypatch.setattr(probe_mod, "run_process", fake_run)
    return holder


class TestProbeMedia:
    def test_fraction_frame_rate_and_basic_fields(self, source_file, fake_ffprobe):
        result = probe_media(source_file, DEFAULT_CONFIG)
        src = result["source"]
        assert src["assetID"] == "clip-one"
        assert src["sourcePath"] == str(source_file.resolve())
        assert len(src["revisionID"]) == 12
        assert src["durationMs"] == 61230
        assert src["durationSec"] == pytest.approx(61.23)
        assert src["frameRate"] == pytest.approx(30000 / 1001)
        assert src["resolution"] == {"width": 1920, "height": 1080}
        assert src["aspectRatio"] == pytest.approx(round(1920 / 1080, 6))
        assert src["analyzedRange"] == {"startMs": 0, "endMs": 61230}
        assert src["audioTracks"] == [
            {
                "trackID": "1",
                "language": "eng",
                "channels": 2,
                "sampleRate": 48000,
                "hasSpeech": "unknown",
                "hasMusic": "unknown",
                "hasEffects": "unknown",
            }
        ]
        assert src["analysisCoverage"][0]["capability"] == "mediaMetadata"
        assert src["analysisCoverage"][0]["status"] == "complete"
        validate_artifact(ArtifactName.MEDIA, result)

    def test_zero_zero_frame_rate_becomes_null(self, source_file, fake_ffprobe):
        fake_ffprobe["payload"] = _ffprobe_payload(avg_frame_rate="0/0")
        result = probe_media(source_file, DEFAULT_CONFIG)
        assert result["source"]["frameRate"] is None

    def test_rotation_90_swaps_resolution(self, source_file, fake_ffprobe):
        fake_ffprobe["payload"] = _ffprobe_payload(rotation=-90)
        result = probe_media(source_file, DEFAULT_CONFIG)
        assert result["source"]["resolution"] == {"width": 1080, "height": 1920}
        assert result["source"]["aspectRatio"] == pytest.approx(round(1080 / 1920, 6))

    def test_no_audio_track(self, source_file, fake_ffprobe):
        fake_ffprobe["payload"] = _ffprobe_payload(audio=False)
        result = probe_media(source_file, DEFAULT_CONFIG)
        assert result["source"]["audioTracks"] == []
        validate_artifact(ArtifactName.MEDIA, result)

    def test_duration_falls_back_to_stream(self, source_file, fake_ffprobe):
        fake_ffprobe["payload"] = _ffprobe_payload(
            format_duration=None, stream_duration="12.500"
        )
        result = probe_media(source_file, DEFAULT_CONFIG)
        assert result["source"]["durationMs"] == 12500
        # 回退来源必须记录在 coverage note 中
        note = result["source"]["analysisCoverage"][0]["note"]
        assert "stream" in note

    def test_format_duration_source_recorded(self, source_file, fake_ffprobe):
        result = probe_media(source_file, DEFAULT_CONFIG)
        note = result["source"]["analysisCoverage"][0]["note"]
        assert "format" in note

    def test_analyzed_range_override(self, source_file, fake_ffprobe):
        result = probe_media(source_file, DEFAULT_CONFIG, analyzed_range=(1000, 2000))
        assert result["source"]["analyzedRange"] == {"startMs": 1000, "endMs": 2000}

    @pytest.mark.parametrize(
        "bad_range",
        [(-1, 2000), (2000, 1000), (1000, 1000), (0, 99999), (61230, 61231)],
    )
    def test_analyzed_range_validation(self, source_file, fake_ffprobe, bad_range):
        with pytest.raises(ValueError):
            probe_media(source_file, DEFAULT_CONFIG, analyzed_range=bad_range)

    def test_missing_ffprobe_raises_capability_unavailable(self, source_file):
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["ffmpeg"]["ffprobePath"] = "/nonexistent/ffprobe-xyz"
        with pytest.raises(CapabilityUnavailableError) as exc_info:
            probe_media(source_file, config)
        assert exc_info.value.capability == "ffprobe"

    def test_ffprobe_failure_raises_capability_unavailable(
        self, source_file, monkeypatch
    ):
        from memoloupe.media.proc import ProcessError

        def failing_run(argv, *, timeout_sec, stdin=None, capture_limit_bytes=None):
            raise ProcessError(
                ProcessResult(
                    argv=tuple(argv),
                    returncode=1,
                    stdout=b"",
                    stderr=b"moov atom not found",
                    elapsed_sec=0.01,
                )
            )

        monkeypatch.setattr(probe_mod, "run_process", failing_run)
        with pytest.raises(CapabilityUnavailableError) as exc_info:
            probe_media(source_file, DEFAULT_CONFIG)
        assert exc_info.value.capability == "ffprobe"
