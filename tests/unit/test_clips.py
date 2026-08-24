"""media/clips.py 单元测试：路径、补齐决策、argv 结构与 normalization（不跑 ffmpeg）。"""

from __future__ import annotations

from memoloupe.media.clips import (
    CLIP_BUILD_VERSION,
    clip_file_rel,
    evidence_clip_argv,
    model_normalization,
    proxy_file_rel,
    proxy_needs_padding,
    proxy_pad_duration_sec,
    model_proxy_argv,
)


def test_version_constant() -> None:
    assert CLIP_BUILD_VERSION == "clips.v1"


def test_clip_paths_forward_slash() -> None:
    assert clip_file_rel("SH0001") == "clips/SH0001.mp4"
    assert proxy_file_rel("SH0002", "a1b2") == "clips/model-proxy/SH0002-a1b2.mp4"
    assert "\\" not in proxy_file_rel("SH0002", "a1b2")


def test_padding_decision() -> None:
    assert proxy_needs_padding(799)
    assert not proxy_needs_padding(800)
    assert not proxy_needs_padding(3203)


def test_pad_duration_to_2000ms() -> None:
    assert proxy_pad_duration_sec(600) == 1.4
    assert proxy_pad_duration_sec(800) == 0.0
    assert proxy_pad_duration_sec(1000) == 0.0


def test_evidence_argv_reencodes_no_stream_copy() -> None:
    argv = evidence_clip_argv(
        "ffmpeg", "/in/src.mp4", 0, 3203, "/out/clips/SH0001.mp4", has_audio=True
    )
    text = " ".join(argv)
    assert "-ss 0.000" in text and "-to 3.203" in text
    assert "libx264" in text and "aac" in text
    assert "copy" not in text  # 禁止 keyframe copy 漂移
    no_audio = evidence_clip_argv(
        "ffmpeg", "/in/src.mp4", 0, 1000, "/out/clips/SH0001.mp4", has_audio=False
    )
    assert "-an" in no_audio


def test_proxy_argv_normalization_and_tpad() -> None:
    argv = model_proxy_argv(
        "ffmpeg", "/in/src.mp4", 0, 600, "/out/p.mp4", has_audio=True, pad_sec=1.4
    )
    text = " ".join(argv)
    assert "scale=720:-2" in text
    assert "fps=10" in text
    assert "tpad=stop_mode=clone:stop_duration=1.4" in text
    assert "libx264" in text and "aac" in text
    unpadded = model_proxy_argv(
        "ffmpeg", "/in/src.mp4", 0, 1000, "/out/p.mp4", has_audio=False, pad_sec=0.0
    )
    assert not any("tpad" in a for a in unpadded)
    assert "-an" in unpadded


def test_model_normalization_structure() -> None:
    norm = model_normalization(
        cache_key="proxy-a1b2",
        file="clips/model-proxy/SH0001-a1b2.mp4",
        padded=True,
    )
    assert norm["cacheKey"] == "proxy-a1b2"
    assert norm["file"] == "clips/model-proxy/SH0001-a1b2.mp4"
    assert norm["padded"] is True
    assert isinstance(norm["strategy"], str)
