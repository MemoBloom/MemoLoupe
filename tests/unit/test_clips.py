"""media/clips.py 单元测试：路径、模态切换、argv 结构与 normalization（不跑 ffmpeg）。"""

from __future__ import annotations

from memoloupe.media.clips import (
    CLIP_BUILD_VERSION,
    SHORT_CLIP_MS,
    clip_file_rel,
    evidence_clip_argv,
    image_proxy_argv,
    image_proxy_file_rel,
    model_normalization,
    model_proxy_argv,
    proxy_file_rel,
)


def test_version_constant() -> None:
    assert CLIP_BUILD_VERSION == "clips.v4"


def test_short_clip_threshold() -> None:
    # 模态切换阈值 2000ms（qwen3.8-flash 视频输入 ≥2s，D-058/D-059）
    assert SHORT_CLIP_MS == 2000


def test_clip_paths_forward_slash() -> None:
    assert clip_file_rel("SH0001") == "clips/SH0001.mp4"
    assert proxy_file_rel("SH0002", "a1b2") == "clips/model-proxy/SH0002-a1b2.mp4"
    assert image_proxy_file_rel("SH0003", "a1b2") == "clips/model-proxy/SH0003-a1b2.jpg"
    assert "\\" not in image_proxy_file_rel("SH0003", "a1b2")


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


def test_proxy_argv_normalization_no_padding() -> None:
    argv = model_proxy_argv(
        "ffmpeg", "/in/src.mp4", 0, 3203, "/out/p.mp4", has_audio=True
    )
    text = " ".join(argv)
    assert "scale=720:-2" in text
    assert "fps=10" in text
    assert "tpad" not in text
    assert "apad" not in text
    assert "-shortest" not in argv
    assert "+faststart" in argv
    assert "libx264" in text and "aac" in text
    no_audio = model_proxy_argv(
        "ffmpeg", "/in/src.mp4", 0, 3203, "/out/p.mp4", has_audio=False
    )
    assert "-an" in no_audio


def test_image_proxy_argv_midframe_jpg() -> None:
    argv = image_proxy_argv("ffmpeg", "/in/src.mp4", 500, "/out/p.jpg")
    text = " ".join(argv)
    assert "-ss 0.500" in text
    assert "-frames:v 1" in text
    assert "scale=720:-2" in text
    assert "-q:v 3" in text
    assert argv[-1] == "/out/p.jpg"


def test_model_normalization_kinds() -> None:
    video = model_normalization(
        cache_key="proxy-a1b2", file="clips/model-proxy/SH0001-a1b2.mp4", kind="video"
    )
    assert video == {
        "strategy": "reencode-w720-fps10",
        "cacheKey": "proxy-a1b2",
        "file": "clips/model-proxy/SH0001-a1b2.mp4",
    }
    image = model_normalization(
        cache_key="proxy-a1b2", file="clips/model-proxy/SH0001-a1b2.jpg", kind="image"
    )
    assert image == {
        "strategy": "frame-midpoint-w720",
        "cacheKey": "proxy-a1b2",
        "file": "clips/model-proxy/SH0001-a1b2.jpg",
    }
