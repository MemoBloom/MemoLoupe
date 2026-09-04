"""media.review_timeline 单元测试：PTS 解析、bin 自适应、降级状态。"""

from __future__ import annotations

import pytest

from memoloupe.media.proc import ProcessError, ProcessResult
from memoloupe.media.review_timeline import (
    effective_bin_duration_ms,
    build_review_timeline,
    build_review_timeline_stub,
    _parse_pts_ms,
)


class _FakePool:
    """脚本化 pool：按序返回 stdout 或抛 ProcessError。"""

    def __init__(self, outcomes: list[bytes | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[list[str]] = []

    def run(self, argv, *, timeout_sec, stdin=None, capture_limit_bytes=None):
        self.calls.append([str(a) for a in argv])
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return ProcessResult(
            argv=tuple(str(a) for a in argv), returncode=0, stdout=outcome,
            stderr=b"", elapsed_sec=0.01,
        )


def _media(*, has_audio: bool = True, start: int = 0, end: int = 4000) -> dict:
    source: dict = {
        "revisionID": "a1b2c3d4e5f6",
        "analyzedRange": {"startMs": start, "endMs": end},
    }
    if has_audio:
        source["audioTracks"] = [{"trackID": "1", "channels": 2, "sampleRate": 44100}]
    return {"source": source, "durationMs": end - start}


class TestParsePts:
    def test_sorts_and_dedups(self) -> None:
        raw = b"0.5\n0.041\n0.0\n0.041\nN/A\n0.083\n"
        assert _parse_pts_ms(raw) == [0, 41, 83, 500]

    def test_negative_dropped(self) -> None:
        assert _parse_pts_ms(b"-0.010\n0.0\n") == [0]

    def test_empty(self) -> None:
        assert _parse_pts_ms(b"") == []


class TestBinAdaptive:
    def test_within_cap_keeps_bin(self) -> None:
        assert effective_bin_duration_ms(60_000, 20, 24_000) == 20

    def test_over_cap_enlarges_bin(self) -> None:
        # 60 分钟 @20ms = 180_000 bins > 24_000 → bin 放大到 ~150ms
        assert effective_bin_duration_ms(3_600_000, 20, 24_000) == 150

    def test_zero_duration(self) -> None:
        assert effective_bin_duration_ms(0, 20, 100) == 20


class TestBuildReviewTimeline:
    def test_no_audio_waveform_unavailable(self) -> None:
        pool = _FakePool([b"0.0\n0.5\n1.5\n"])
        doc = build_review_timeline("src.mp4", _media(has_audio=False), {}, pool=pool)
        assert doc["status"] == "complete"
        assert doc["videoFrames"]["timingMode"] == "pts-index"
        assert doc["videoFrames"]["ptsMs"] == [0, 500, 1500]
        # 无音轨 → 显式 unavailable，不是空波形
        assert doc["waveform"]["status"] == "unavailable"
        assert "peaks" not in doc["waveform"]
        assert doc["waveform"]["channelMode"] == "unavailable"

    def test_pts_filtered_to_analyzed_range(self) -> None:
        # analyzedRange 之外的 PTS 被过滤
        pool = _FakePool([b"-0.1\n0.0\n0.5\n9.5\n"])
        doc = build_review_timeline(
            "src.mp4", _media(has_audio=False, start=0, end=4000), {}, pool=pool
        )
        assert doc["videoFrames"]["ptsMs"] == [0, 500]

    def test_ffprobe_failure_is_partial_not_exception(self) -> None:
        err = ProcessError(
            ProcessResult(argv=("ffprobe",), returncode=1, stdout=b"", stderr=b"boom",
                          elapsed_sec=0.01)
        )
        pool = _FakePool([err])
        doc = build_review_timeline("src.mp4", _media(has_audio=False), {}, pool=pool)
        assert doc["status"] == "partial"
        assert doc["videoFrames"]["status"] == "failed"
        assert "帧 PTS 提取失败" in doc["videoFrames"]["reason"]

    def test_empty_pts_is_unavailable(self) -> None:
        pool = _FakePool([b""])
        doc = build_review_timeline("src.mp4", _media(has_audio=False), {}, pool=pool)
        assert doc["videoFrames"]["status"] == "unavailable"
        assert doc["videoFrames"]["timingMode"] == "unavailable"
        assert doc["status"] == "complete"

    def test_waveform_bins_computed(self) -> None:
        # 16kHz 采样：bin = 20ms = 320 样本；3 bins 的 PCM：
        # 第 1 bin 满幅正、第 2 bin 静音、第 3 bin 满幅负。
        sample_rate = 16000
        bin_ms = 20
        samples_per_bin = sample_rate * bin_ms // 1000  # 320
        import array

        pcm = array.array("h", [16000] * samples_per_bin)
        pcm.extend(array.array("h", [0] * samples_per_bin))
        pcm.extend(array.array("h", [-16000] * samples_per_bin))
        pool = _FakePool([b"0.0\n", pcm.tobytes()])
        config = {"ffmpeg": {"ffmpegPath": "ffmpeg", "ffprobePath": "ffprobe"},
                  "reviewTimeline": {"waveformSampleRate": sample_rate,
                                     "waveformBinMs": bin_ms}}
        doc = build_review_timeline(
            "src.mp4", _media(end=samples_per_bin * 3 * 1000 // sample_rate),
            config, pool=pool,
        )
        assert doc["waveform"]["status"] == "complete"
        assert doc["waveform"]["binCount"] == 3
        peaks = doc["waveform"]["peaks"]
        assert peaks[0][1] == pytest.approx(16000 / 32768, abs=0.01)
        assert peaks[1][0] == peaks[1][1] == 0.0
        assert peaks[2][0] == pytest.approx(-16000 / 32768, abs=0.01)

    def test_waveform_failure_is_partial(self) -> None:
        err = ProcessError(
            ProcessResult(argv=("ffmpeg",), returncode=1, stdout=b"", stderr=b"no audio",
                          elapsed_sec=0.01)
        )
        pool = _FakePool([b"0.0\n", err])
        doc = build_review_timeline("src.mp4", _media(), {}, pool=pool)
        assert doc["status"] == "partial"
        assert doc["waveform"]["status"] == "failed"


class TestStub:
    def test_stub_is_explicitly_failed(self) -> None:
        doc = build_review_timeline_stub(_media(), {}, "用户显式跳过")
        assert doc["status"] == "failed"
        assert doc["videoFrames"]["status"] == "unavailable"
        assert doc["waveform"]["status"] == "unavailable"
        assert "用户显式跳过" in doc["videoFrames"]["reason"]
