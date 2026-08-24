"""媒体探测（docs/03 §2.2）。

用 ffprobe JSON 输出组装符合 ``schemas/media.json`` 的 dict：

- duration 优先 ``format.duration``，不可靠时回退到视频流 ``duration``，
  来源记录到 ``analysisCoverage[].note``；
- 帧率用 :func:`~memoloupe.core.time_ranges.parse_fraction` 解析
  ``avg_frame_rate``，``"0/0"`` 或缺失写 ``None``，绝不写 0；
- 分辨率应用 display matrix / rotate 旋转（90/270 度交换宽高）；
- 秒 → 毫秒一律走 :func:`~memoloupe.core.time_ranges.seconds_to_ms`。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from memoloupe.core.errors import CapabilityUnavailableError
from memoloupe.core.hashing import content_revision_id
from memoloupe.core.time_ranges import parse_fraction, seconds_to_ms
from memoloupe.media.concurrency import FFmpegPool
from memoloupe.media.proc import ProcessError, run_process


def probe_media(
    source: Path,
    config: dict,
    *,
    pool: FFmpegPool | None = None,
    analyzed_range: tuple[int, int] | None = None,
) -> dict:
    """探测源视频元数据，返回符合 media.json 的 dict。

    ffprobe 不存在、非零退出或输出无法解析时抛
    :class:`CapabilityUnavailableError`；``analyzed_range`` 越界抛 ValueError。
    """
    ffmpeg_cfg = config.get("ffmpeg", {})
    ffprobe_path = str(ffmpeg_cfg.get("ffprobePath", "ffprobe"))
    timeout_sec = float(ffmpeg_cfg.get("probeTimeoutSec", 30.0))

    source = Path(source)
    argv = [
        ffprobe_path,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(source),
    ]
    runner = pool.run if pool is not None else run_process
    try:
        result = runner(argv, timeout_sec=timeout_sec)
    except (ProcessError, OSError) as exc:
        # OSError 覆盖 FileNotFoundError（ffprobe 不在 PATH）。
        raise CapabilityUnavailableError("ffprobe", str(exc)) from exc
    try:
        data = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapabilityUnavailableError(
            "ffprobe", f"ffprobe 输出无法解析为 JSON: {exc}"
        ) from exc

    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError(f"源文件无视频流: {source}")

    duration_sec, duration_source = _resolve_duration(data.get("format") or {}, video)
    duration_ms = seconds_to_ms(duration_sec)

    width, height = _apply_rotation(video)
    aspect_ratio = round(width / height, 6)
    frame_rate = parse_fraction(str(video.get("avg_frame_rate", "")))

    if analyzed_range is None:
        start_ms, end_ms = 0, duration_ms
    else:
        start_ms, end_ms = int(analyzed_range[0]), int(analyzed_range[1])
        if not (0 <= start_ms < end_ms <= duration_ms):
            raise ValueError(
                f"analyzed_range 必须满足 0 <= start < end <= durationMs"
                f"({duration_ms}): {analyzed_range!r}"
            )

    return {
        "source": {
            "assetID": source.stem,
            "sourcePath": str(source.resolve()),
            "revisionID": content_revision_id(source),
            "durationMs": duration_ms,
            "durationSec": duration_ms / 1000.0,
            "frameRate": frame_rate,
            "resolution": {"width": width, "height": height},
            "aspectRatio": aspect_ratio,
            "audioTracks": [_audio_track(s) for s in streams if s.get("codec_type") == "audio"],
            "analyzedRange": {"startMs": start_ms, "endMs": end_ms},
            "analysisCoverage": [
                {
                    "capability": "mediaMetadata",
                    "status": "complete",
                    "note": f"ffprobe; duration={duration_source}",
                }
            ],
        }
    }


def _parse_duration(value: Any) -> float | None:
    """解析 ffprobe duration 字段；缺失/"N/A"/非正数视为不可靠。"""
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _resolve_duration(fmt: dict, video: dict) -> tuple[float, str]:
    """duration 优先 format，回退视频流；两者都不可靠时抛 ValueError。"""
    format_duration = _parse_duration(fmt.get("duration"))
    if format_duration is not None:
        return format_duration, "format"
    stream_duration = _parse_duration(video.get("duration"))
    if stream_duration is not None:
        return stream_duration, "stream"
    raise ValueError("ffprobe 未提供可靠时长（format 与视频流 duration 均缺失）")


def _display_rotation(stream: dict) -> int:
    """读取 display matrix / rotate 旋转角度（度），无法解析时为 0。"""
    for side_data in stream.get("side_data_list") or []:
        if isinstance(side_data, dict) and "rotation" in side_data:
            try:
                return int(side_data["rotation"])
            except (TypeError, ValueError):
                pass
    tag = (stream.get("tags") or {}).get("rotate")
    if tag is not None:
        try:
            return int(tag)
        except (TypeError, ValueError):
            pass
    return 0


def _apply_rotation(stream: dict) -> tuple[int, int]:
    """返回应用显示旋转后的 (width, height)；90/270 度交换宽高。"""
    width = int(stream["width"])
    height = int(stream["height"])
    if _display_rotation(stream) % 360 in (90, 270):
        return height, width
    return width, height


def _audio_track(stream: dict) -> dict:
    tags = stream.get("tags") or {}
    return {
        "trackID": str(stream.get("index", 0)),
        "language": tags.get("language") or "unknown",
        "channels": int(stream.get("channels", 1)),
        "sampleRate": int(float(stream.get("sample_rate", 1))),
        "hasSpeech": "unknown",
        "hasMusic": "unknown",
        "hasEffects": "unknown",
    }
