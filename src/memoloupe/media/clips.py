"""证据 clip 与模型代理构建（docs/03 §2.6，unified-media.json 的 clips[]）。

- 证据 clip：按 final 区间精确重编码（libx264/aac），避免 keyframe copy 漂移；
- 模型代理：统一宽 720。短于 2000ms 的镜头输出中点帧 JPG 图像代理
  （静帧对短镜头更具代表性，且绕开云端模型的最短视频约束，D-059）；
  其余输出 fps 10 的重编码视频代理。代理只影响模型输入，不改变证据
  clip 和镜头边界。
"""

from __future__ import annotations

import json
from pathlib import Path

from memoloupe.core.hashing import content_revision_id
from memoloupe.core.ids import validate_shot_id
from memoloupe.core.time_ranges import seconds_to_ms
from memoloupe.media.frames import representative_time_ms

CLIP_BUILD_VERSION = "clips.v4"

# 模型代理统一参数（docs/03 §2.6 恢复策略）
PROXY_WIDTH = 720
PROXY_FPS = 10
#: 模态切换阈值：低于 2000ms 的镜头用中点帧图像代理（qwen3.8-flash 要求
#: 视频输入 ≥2s，D-058；短镜头改用图像由 D-059 决策）。
SHORT_CLIP_MS = 2000
#: 图像代理 JPEG 质量（ffmpeg -q:v，越小越好）
IMAGE_PROXY_JPEG_QUALITY = 3


def clip_file_rel(shot_id: str) -> str:
    return f"clips/{validate_shot_id(shot_id)}.mp4"


def proxy_file_rel(shot_id: str, cache_key4: str) -> str:
    return f"clips/model-proxy/{validate_shot_id(shot_id)}-{cache_key4}.mp4"


def image_proxy_file_rel(shot_id: str, cache_key4: str) -> str:
    return f"clips/model-proxy/{validate_shot_id(shot_id)}-{cache_key4}.jpg"


def _sec(ms: int) -> str:
    return f"{ms / 1000:.3f}"


def evidence_clip_argv(
    ffmpeg: str, source: str, start_ms: int, end_ms: int, out_path: str, *, has_audio: bool
) -> list[str]:
    """证据 clip：-ss/-to 放在 -i 前做精确 seek，重编码保证边界准确。"""
    argv = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", _sec(start_ms), "-to", _sec(end_ms), "-i", source,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
    ]
    if has_audio:
        argv += ["-c:a", "aac", "-b:a", "128k"]
    else:
        argv += ["-an"]
    argv.append(out_path)
    return argv


def model_proxy_argv(
    ffmpeg: str,
    source: str,
    start_ms: int,
    end_ms: int,
    out_path: str,
    *,
    has_audio: bool,
) -> list[str]:
    """模型代理：宽 720、fps 10，输出 faststart MP4。"""
    vf = f"scale={PROXY_WIDTH}:-2,fps={PROXY_FPS}"
    argv = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", _sec(start_ms), "-to", _sec(end_ms), "-i", source,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-pix_fmt", "yuv420p",
    ]
    if has_audio:
        argv += ["-c:a", "aac", "-b:a", "96k"]
    else:
        argv += ["-an"]
    argv += ["-movflags", "+faststart"]
    argv.append(out_path)
    return argv


def image_proxy_argv(ffmpeg: str, source: str, time_ms: int, out_path: str) -> list[str]:
    """图像代理：镜头中点单帧，宽 720 JPEG。"""
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{time_ms / 1000:.3f}", "-i", source,
        "-frames:v", "1",
        "-vf", f"scale={PROXY_WIDTH}:-2",
        "-q:v", str(IMAGE_PROXY_JPEG_QUALITY),
        out_path,
    ]


def model_normalization(*, cache_key: str, file: str, kind: str) -> dict:
    if kind == "image":
        strategy = f"frame-midpoint-w{PROXY_WIDTH}"
    else:
        strategy = f"reencode-w{PROXY_WIDTH}-fps{PROXY_FPS}"
    return {
        "strategy": strategy,
        "cacheKey": cache_key,
        "file": file,
    }


def _probe_duration_ms(path: Path, config: dict, pool) -> int:
    """ffprobe 读容器时长（秒 → 毫秒走唯一入口 seconds_to_ms）。"""
    result = pool.run(
        [
            config["ffmpeg"]["ffprobePath"],
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ],
        timeout_sec=float(config["ffmpeg"]["probeTimeoutSec"]),
    )
    data = json.loads(result.stdout.decode("utf-8"))
    return seconds_to_ms(data["format"]["duration"])


def build_clips(
    source: Path,
    shots: list[dict],
    has_audio: bool,
    config: dict,
    out_dir: Path,
    *,
    pool,
) -> list[dict]:
    """为每个镜头构建证据 clip 和模型代理，返回 unified-media.json 的 clips[] 项。

    任一 clip 构建失败视为硬错误（ProcessError 向上抛），由编排层决定降级；
    这里不生成指向不存在文件的路径。
    """
    source = Path(source)
    out_dir = Path(out_dir)
    clips_dir = out_dir / "clips"
    proxy_dir = clips_dir / "model-proxy"
    proxy_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg = config["ffmpeg"]["ffmpegPath"]
    timeout = float(config["ffmpeg"]["clipTimeoutSec"])
    # 代理缓存键：内容 revision 前 4 位 + 归一化版本，与 unified-media 示例一致
    revision4 = content_revision_id(source)[:4]
    cache_key = f"proxy-{revision4}"

    items: list[dict] = []
    for shot in shots:
        shot_id = validate_shot_id(shot["shotID"])
        start_ms = int(shot["finalStartMs"])
        end_ms = int(shot["finalEndMs"])
        duration_ms = end_ms - start_ms

        evidence_rel = clip_file_rel(shot_id)
        pool.run(
            evidence_clip_argv(
                ffmpeg, str(source), start_ms, end_ms,
                str(clips_dir / f"{shot_id}.mp4"), has_audio=has_audio,
            ),
            timeout_sec=timeout,
        )

        if duration_ms < SHORT_CLIP_MS:
            frame_ms = representative_time_ms(start_ms, end_ms)
            proxy_rel = image_proxy_file_rel(shot_id, revision4)
            proxy_path = proxy_dir / f"{shot_id}-{revision4}.jpg"
            pool.run(
                image_proxy_argv(ffmpeg, str(source), frame_ms, str(proxy_path)),
                timeout_sec=timeout,
            )
            # 静帧没有可探测时长；语义为"模型输入所代表的镜头时长"
            model_duration_ms = duration_ms
            kind = "image"
        else:
            proxy_rel = proxy_file_rel(shot_id, revision4)
            proxy_path = proxy_dir / f"{shot_id}-{revision4}.mp4"
            pool.run(
                model_proxy_argv(
                    ffmpeg, str(source), start_ms, end_ms, str(proxy_path),
                    has_audio=has_audio,
                ),
                timeout_sec=timeout,
            )
            model_duration_ms = _probe_duration_ms(proxy_path, config, pool)
            kind = "video"

        items.append(
            {
                "shotID": shot_id,
                "startMs": start_ms,
                "endMs": end_ms,
                "durationMs": duration_ms,
                "file": evidence_rel,
                "modelFile": proxy_rel,
                "modelDurationMs": model_duration_ms,
                "modelNormalization": model_normalization(
                    cache_key=cache_key, file=proxy_rel, kind=kind
                ),
            }
        )
    return items
