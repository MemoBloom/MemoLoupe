"""证据 clip 与模型代理构建（docs/03 §2.6，unified-media.json 的 clips[]）。

- 证据 clip：按 final 区间精确重编码（libx264/aac），避免 keyframe copy 漂移；
- 模型代理：统一宽 720、fps 10；短于 800ms 的 clip 用 tpad 补帧到至少
  2000ms（只影响模型输入，不改变证据 clip 和镜头边界）。
"""

from __future__ import annotations

import json
from pathlib import Path

from memoloupe.core.hashing import content_revision_id
from memoloupe.core.ids import validate_shot_id
from memoloupe.core.time_ranges import seconds_to_ms

CLIP_BUILD_VERSION = "clips.v1"

# 模型代理统一参数（docs/03 §2.6 恢复策略）
PROXY_WIDTH = 720
PROXY_FPS = 10
SHORT_CLIP_MS = 800
PADDED_MIN_MS = 2000


def clip_file_rel(shot_id: str) -> str:
    return f"clips/{validate_shot_id(shot_id)}.mp4"


def proxy_file_rel(shot_id: str, cache_key4: str) -> str:
    return f"clips/model-proxy/{validate_shot_id(shot_id)}-{cache_key4}.mp4"


def proxy_needs_padding(duration_ms: int) -> bool:
    return duration_ms < SHORT_CLIP_MS


def proxy_pad_duration_sec(duration_ms: int) -> float:
    """tpad 需要补的秒数；不需要补齐时为 0。"""
    if not proxy_needs_padding(duration_ms):
        return 0.0
    return (PADDED_MIN_MS - duration_ms) / 1000.0


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
    pad_sec: float,
) -> list[str]:
    """模型代理：宽 720（-2 保持纵横比且高度为偶数）、fps 10，可选 tpad 补帧。"""
    vf = f"scale={PROXY_WIDTH}:-2,fps={PROXY_FPS}"
    if pad_sec > 0:
        vf += f",tpad=stop_mode=clone:stop_duration={pad_sec:g}"
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
    argv.append(out_path)
    return argv


def model_normalization(*, cache_key: str, file: str, padded: bool) -> dict:
    strategy = f"reencode-w{PROXY_WIDTH}-fps{PROXY_FPS}"
    if padded:
        strategy += f"+tpad-clone-{PADDED_MIN_MS}ms"
    return {
        "strategy": strategy,
        "cacheKey": cache_key,
        "file": file,
        "padded": padded,
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

        proxy_rel = proxy_file_rel(shot_id, revision4)
        pad_sec = proxy_pad_duration_sec(duration_ms)
        proxy_path = proxy_dir / f"{shot_id}-{revision4}.mp4"
        pool.run(
            model_proxy_argv(
                ffmpeg, str(source), start_ms, end_ms, str(proxy_path),
                has_audio=has_audio, pad_sec=pad_sec,
            ),
            timeout_sec=timeout,
        )
        model_duration_ms = _probe_duration_ms(proxy_path, config, pool)

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
                    cache_key=cache_key, file=proxy_rel, padded=pad_sec > 0
                ),
            }
        )
    return items
