"""services/asr_local 纯函数单元测试：段合并、窗口打包、拼接轴映射。"""

from __future__ import annotations

from memoloupe.services.asr_local import (
    build_concat_map,
    concat_to_source,
    map_concat_segments,
    merge_vad_segments,
    pack_windows,
)


def test_merge_vad_segments_merges_close_and_sorts():
    ts = [(2.0, 3.0), (0.5, 1.0), (1.2, 1.8), (5.0, 6.0)]
    # 排序后 (500,1000)/(1200,1800)/(2000,3000) 相邻间隔均 200ms <= 300ms，
    # 三段级联合并为 (500,3000)；(5000,6000) 间隔 2000ms 保持独立。
    assert merge_vad_segments(ts, merge_gap_ms=300) == [
        (500, 3000),
        (5000, 6000),
    ]


def test_merge_vad_segments_gap_beyond_threshold_stays_split():
    ts = [(0.5, 1.0), (1.5, 2.0)]
    assert merge_vad_segments(ts, merge_gap_ms=300) == [(500, 1000), (1500, 2000)]


def test_merge_vad_segments_drops_invalid():
    assert merge_vad_segments(
        [(1.0, 1.0), (2.0, 1.0), (3.0, 4.0)], merge_gap_ms=0
    ) == [(3000, 4000)]


def test_pack_windows_splits_overlong_and_pads():
    segs = [(1000, 5000), (6000, 20000), (25000, 26000)]
    # window 上限 10s：第一段 + 第二段拼起来 19s 超限 → 拆窗
    windows = pack_windows(segs, window_ms=10_000, pad_ms=200, total_ms=30_000)
    assert windows == [(800, 5200), (5800, 20200), (24800, 26200)]


def test_pack_windows_clamps_to_total():
    windows = pack_windows([(0, 1000)], window_ms=10_000, pad_ms=200, total_ms=1100)
    assert windows == [(0, 1100)]


def test_concat_map_roundtrip():
    windows = [(800, 5200), (5800, 20200)]
    entries, total = build_concat_map(windows, silence_ms=500)
    # 第一窗 4400ms + 静音 500ms + 第二窗 14400ms
    assert total == 4400 + 500 + 14400
    assert entries == [(0, 800, 4400), (4900, 5800, 14400)]
    # 拼接轴 1000ms → 源轴 1800ms
    assert concat_to_source(entries, 1000) == 1800
    # 落在静音区（4500ms）→ clamp 到前一窗末尾 5200ms
    assert concat_to_source(entries, 4500) == 5200
    # 第二窗起点
    assert concat_to_source(entries, 4900) == 5800


def test_map_concat_segments_offsets_and_filters():
    entries = [(0, 800, 4400)]
    whisper_segments = [
        {"start": 0.5, "end": 1.5, "text": " 你好 "},
        {"start": 2.0, "end": 2.0, "text": "无时长"},  # end<=start 丢弃
        {"start": 3.0, "end": 3.5, "text": "   "},  # 空文本丢弃
    ]
    out = map_concat_segments(whisper_segments, entries=entries, range_start_ms=10_000)
    assert out == [
        {
            "startMs": 10_000 + 800 + 500,
            "endMs": 10_000 + 800 + 1500,
            "text": "你好",
            "speaker": None,
            "confidence": None,
        },
    ]


import wave
from pathlib import Path

import pytest

from memoloupe.core.errors import CapabilityUnavailableError
from memoloupe.services.asr import ASRRequest, build_asr_service
from memoloupe.services.asr_local import (
    LOCAL_ASR_VERSION,
    PROVIDER_LOCAL,
    LocalFireRedVadMlxASR,
)


def _write_wav(path: Path, total_ms: int = 10_000) -> Path:
    """写 16kHz mono s16le 静音 wav。"""
    frames = b"\x00\x00" * (total_ms * 16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(frames)
    return path


def _service(tmp_path: Path, **hooks) -> LocalFireRedVadMlxASR:
    cfg = {
        "vad": {
            "modelDir": None,
            "speechThreshold": 0.4,
            "smoothWindowSize": 5,
            "minSpeechFrame": 20,
            "maxSpeechFrame": 2000,
            "minSilenceFrame": 20,
        },
        "whisper": {
            "model": "mlx-community/whisper-large-v3-turbo",
            "wordTimestamps": True,
        },
        "mergeGapMs": 300,
        "windowSec": 30,
        "windowPadMs": 200,
    }
    return LocalFireRedVadMlxASR(
        asr_config=cfg, ffmpeg_path="ffmpeg", decode_timeout_sec=60.0, **hooks
    )


def test_transcribe_full_flow_with_fakes(tmp_path: Path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fake")

    def fake_decode(media_path, start_ms, end_ms, work_dir):
        assert (start_ms, end_ms) == (1000, 11_000)
        return _write_wav(Path(work_dir) / "a.wav"), 10_000

    def fake_vad(wav_path):
        return [(0.5, 2.0), (2.2, 4.0)]  # 间隔 200ms 合并 → 一窗

    def fake_transcribe(audio, language):
        assert language == "zh"
        return {
            "segments": [{"start": 0.3, "end": 1.0, "text": "你好"}],
            "language": "zh",
        }

    service = _service(
        tmp_path,
        decode_fn=fake_decode,
        vad_detect_fn=fake_vad,
        transcribe_fn=fake_transcribe,
    )
    result = service.transcribe(
        media, ASRRequest(language="zh", start_ms=1000, end_ms=11_000)
    )
    # VAD 窗 [300, 4200]（pad 200）→ whisper 段 (0.3,1.0)s 映射回源轴
    # 300+300=600 / 300+1000=1300，再加 analyzedRange 起点 1000 → 1600/2300
    assert [dict(s) for s in result.segments] == [
        {
            "startMs": 1600,
            "endMs": 2300,
            "text": "你好",
            "speaker": None,
            "confidence": None,
        }
    ]
    assert result.raw_extras["local"]["provider"] == PROVIDER_LOCAL
    assert result.raw_extras["local"]["version"] == LOCAL_ASR_VERSION
    assert result.raw_extras["local"]["whisper"]["windowCount"] == 1


def test_transcribe_no_speech_returns_empty(tmp_path: Path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fake")
    service = _service(
        tmp_path,
        decode_fn=lambda *a: (_write_wav(Path(a[3]) / "a.wav"), 10_000),
        vad_detect_fn=lambda wav_path: [],
        transcribe_fn=lambda audio, language: pytest.fail("不应调用 whisper"),
    )
    result = service.transcribe(media, ASRRequest(start_ms=0, end_ms=10_000))
    assert result.segments == ()
    assert result.raw_extras["local"]["vad"]["segments"] == []


def test_missing_dependency_raises_capability_unavailable(tmp_path: Path):
    try:
        import fireredvad  # noqa: F401
        import mlx_whisper  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("本地依赖已安装，无法验证缺依赖降级")
    service = _service(tmp_path)  # 不注入钩子 → 真实懒加载路径
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fake")
    with pytest.raises(CapabilityUnavailableError):
        service.transcribe(media, ASRRequest(start_ms=0, end_ms=None))


def test_build_asr_service_local_provider():
    config = {
        "asr": {
            "enabled": True,
            "provider": PROVIDER_LOCAL,
            "vad": {},
            "whisper": {},
            "mergeGapMs": 300,
            "windowSec": 30,
            "windowPadMs": 200,
        },
        "ffmpeg": {"ffmpegPath": "ffmpeg", "scanTimeoutSec": 600.0},
    }
    service = build_asr_service(config)
    assert isinstance(service, LocalFireRedVadMlxASR)


def test_build_asr_service_local_disabled():
    config = {"asr": {"enabled": False, "provider": PROVIDER_LOCAL}}
    assert build_asr_service(config) is None
