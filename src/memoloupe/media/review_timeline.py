"""审片时间线确定性索引（Phase 06-01，schemas/review-timeline.json）。

两类确定性数据，均不承载语义判断：

1. ``videoFrames``：ffprobe 逐帧真实 PTS（毫秒）。VFR 视频必须使用真实
   时间戳，不得用平均帧率伪造；无可靠索引时显式 ``unavailable``，
   UI 降级为平均帧率近似并标注。
2. ``waveform``：ffmpeg 分块解码音频 → 固定上限的归一化 min/max envelope。
   只保存绘制所需 envelope，不保存 PCM；无音轨时显式 ``unavailable``。

所有降级都写显式状态与原因，绝不伪造空索引或空波形（docs/03 降级矩阵）。
"""

from __future__ import annotations

import math
from pathlib import Path

from memoloupe.media.proc import ProcessError

REVIEW_TIMELINE_VERSION = "review-timeline.v1"

#: 波形解码统一转 mono s16le；int16 满幅用于归一化。
_INT16_SCALE = 32768.0
#: envelope 数值小数位（JSON 体积与精度平衡）。
_PEAK_DECIMALS = 3


def effective_bin_duration_ms(duration_ms: int, bin_ms: int, max_bins: int) -> int:
    """波形 bin 时长自适应：不超过 max_bins 时保持 bin_ms，否则放大。"""
    if duration_ms <= 0:
        return max(bin_ms, 1)
    needed = math.ceil(duration_ms / max(bin_ms, 1))
    if needed <= max_bins:
        return max(bin_ms, 1)
    return max(int(math.ceil(duration_ms / max_bins)), 1)


def _parse_pts_ms(stdout: bytes) -> list[int]:
    """ffprobe csv 输出（每行一个 pts_time 秒浮点）→ 排序去重毫秒列表。

    - 排序：B 帧解码序不等于显示序；保留真实 PTS 值、仅恢复单调序；
    - 去重：重复展示帧只保留一个索引点。
    """
    values: list[int] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        token = line.strip().split(",")[0].strip()
        if not token or token == "N/A":
            continue
        try:
            seconds = float(token)
        except ValueError:
            continue
        ms = int(round(seconds * 1000))
        if ms >= 0:
            values.append(ms)
    values.sort()
    deduped: list[int] = []
    for ms in values:
        if not deduped or deduped[-1] != ms:
            deduped.append(ms)
    return deduped


def probe_frame_pts(
    source: Path,
    ffprobe_path: str,
    *,
    start_ms: int,
    end_ms: int,
    timeout_sec: float,
    pool,
) -> list[int]:
    """返回分析范围内的真实帧 PTS 毫秒列表。

    进程失败抛 :class:`ProcessError`；成功但无 PTS 返回空列表
    （调用方落 unavailable 降级，不视为异常）。
    """
    argv = [
        ffprobe_path,
        "-hide_banner",
        "-loglevel", "error",
        "-select_streams", "v:0",
        "-show_entries", "frame=pts_time",
        "-of", "csv=p=0",
        str(source),
    ]
    result = pool.run(argv, timeout_sec=timeout_sec)
    return [ms for ms in _parse_pts_ms(result.stdout) if start_ms <= ms < end_ms]


def build_waveform(
    source: Path,
    ffmpeg_path: str,
    *,
    duration_ms: int,
    sample_rate: int,
    bin_ms: int,
    chunk_sec: int,
    timeout_sec: float,
    pool,
) -> list[list[float]]:
    """分块解码音频并生成归一化 min/max envelope。

    每块独立进程（``-ss/-t``），流式累积 bin 极值，任意时刻内存只含
    一个 chunk 的 PCM（默认 ≤ 120s × 16kHz × 2B ≈ 3.8MB）。
    音频短于/超出 analyzedRange 的块自然为空数据，不视为失败。
    """
    import array

    total_bins = max(1, math.ceil(duration_ms / max(bin_ms, 1)))
    mins = [1.0] * total_bins
    maxs = [-1.0] * total_bins
    filled = 0

    offset_ms = 0
    while offset_ms < duration_ms:
        chunk_ms = min(chunk_sec * 1000, duration_ms - offset_ms)
        argv = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-ss", f"{offset_ms / 1000:.3f}",
            "-t", f"{chunk_ms / 1000:.3f}",
            "-i", str(source),
            "-vn",
            "-ac", "1",
            "-ar", str(sample_rate),
            "-f", "s16le",
            "pipe:1",
        ]
        result = pool.run(argv, timeout_sec=timeout_sec, capture_limit_bytes=None)
        samples = array.array("h")
        samples.frombytes(result.stdout[: len(result.stdout) // 2 * 2])
        samples_per_bin = max(sample_rate * bin_ms // 1000, 1)
        base_index = offset_ms // bin_ms
        for i, sample in enumerate(samples):
            index = base_index + i // samples_per_bin
            if index >= total_bins:
                break
            value = sample / _INT16_SCALE
            if value < mins[index]:
                mins[index] = value
            if value > maxs[index]:
                maxs[index] = value
            if index >= filled:
                filled = index + 1
        offset_ms += chunk_ms

    # 未覆盖任何采样的 bin 不伪造数据：写 0 电平（检测不到非零样本即静音）。
    peaks: list[list[float]] = []
    for i in range(total_bins):
        if i >= filled or mins[i] > maxs[i]:
            lo = hi = 0.0
        else:
            lo, hi = mins[i], maxs[i]
        peaks.append(
            [
                round(max(lo, -1.0), _PEAK_DECIMALS),
                round(min(hi, 1.0), _PEAK_DECIMALS),
            ]
        )
    return peaks


def build_review_timeline(
    source: Path,
    media: dict,
    config: dict,
    *,
    pool,
) -> dict:
    """生成 ``raw/review-timeline.json`` 文档（确定性，不含语义）。"""
    ffmpeg_cfg = config.get("ffmpeg", {})
    rt_cfg = config.get("reviewTimeline", {})
    ffprobe_path = str(ffmpeg_cfg.get("ffprobePath", "ffprobe"))
    ffmpeg_path = str(ffmpeg_cfg.get("ffmpegPath", "ffmpeg"))
    timeout_frames = float(rt_cfg.get("framePtsTimeoutSec", 120.0))
    timeout_wave = float(rt_cfg.get("waveformTimeoutSec", 120.0))
    sample_rate = int(rt_cfg.get("waveformSampleRate", 16000))
    bin_ms = int(rt_cfg.get("waveformBinMs", 20))
    max_bins = int(rt_cfg.get("maxWaveformBins", 24000))
    chunk_sec = int(rt_cfg.get("waveformChunkSec", 120))

    analyzed = (media.get("source") or {}).get(
        "analyzedRange", {"startMs": 0, "endMs": 0}
    )
    start_ms = int(analyzed.get("startMs", 0))
    end_ms = int(analyzed.get("endMs", 0))
    duration_ms = max(end_ms - start_ms, 0)
    has_audio = bool((media.get("source") or {}).get("audioTracks"))

    warnings: list[str] = []

    # 1. videoFrames --------------------------------------------------------
    pts_ms: list[int] = []
    frames_status = "failed"
    frames_reason: str | None = "未运行"
    try:
        pts_ms = probe_frame_pts(
            source,
            ffprobe_path,
            start_ms=start_ms,
            end_ms=end_ms,
            timeout_sec=timeout_frames,
            pool=pool,
        )
        if pts_ms:
            frames_status = "complete"
            frames_reason = None
        else:
            # 确定性降级：无可靠逐帧 PTS，UI 按平均帧率近似并标注。
            frames_status = "unavailable"
            frames_reason = "ffprobe 未返回分析范围内的帧 PTS"
    except ProcessError as exc:
        frames_reason = f"帧 PTS 提取失败：{exc}"
        warnings.append(frames_reason)

    # 波形 bin 上限：超长视频自适应增大 bin 时长（决策 D-0xx）。
    eff_bin_ms = effective_bin_duration_ms(duration_ms, bin_ms, max_bins)

    # 2. waveform -------------------------------------------------------------
    peaks: list[list[float]] = []
    wave_status = "unavailable"
    wave_reason: str | None = "未运行"
    if not has_audio:
        # 确定性降级：无音轨不是失败，UI 显示显式状态而非空波形。
        wave_reason = "源无音轨（audioTracks 为空）"
    else:
        try:
            peaks = build_waveform(
                source,
                ffmpeg_path,
                duration_ms=duration_ms,
                sample_rate=sample_rate,
                bin_ms=eff_bin_ms,
                chunk_sec=chunk_sec,
                timeout_sec=timeout_wave,
                pool=pool,
            )
            wave_status = "complete"
            wave_reason = None
        except ProcessError as exc:
            wave_status = "failed"
            wave_reason = f"波形提取失败：{exc}"
            warnings.append(wave_reason)

    if frames_status == "failed" or wave_status == "failed":
        top_status = "partial"
    elif frames_status == "complete" and wave_status == "complete":
        top_status = "complete"
    else:
        top_status = "complete"

    video_frames: dict = {"status": frames_status}
    if frames_status == "complete":
        video_frames["timingMode"] = "pts-index"
        video_frames["frameCount"] = len(pts_ms)
        video_frames["ptsMs"] = pts_ms
    else:
        video_frames["timingMode"] = "unavailable"
    if frames_reason:
        video_frames["reason"] = frames_reason

    waveform: dict = {"status": wave_status}
    if wave_status == "complete":
        waveform["channelMode"] = "mono-mixdown"
        waveform["binDurationMs"] = eff_bin_ms
        waveform["binCount"] = len(peaks)
        waveform["peaks"] = peaks
    else:
        waveform["channelMode"] = "unavailable"
    if wave_reason:
        waveform["reason"] = wave_reason

    doc: dict = {
        "schemaVersion": 1,
        "status": top_status,
        "sourceRevisionID": media.get("source", {}).get("revisionID", ""),
        "analysis": {
            "method": "ffprobe-frame-pts-and-ffmpeg-audio-envelope",
            "algorithmVersion": REVIEW_TIMELINE_VERSION,
            "analyzedRange": {"startMs": start_ms, "endMs": end_ms},
        },
        "videoFrames": video_frames,
        "waveform": waveform,
    }
    if warnings:
        doc["warnings"] = warnings
    return doc


def build_review_timeline_stub(media: dict, config: dict, reason: str) -> dict:
    """``--skip build_review_timeline`` 降级 stub：状态显式、索引为空。"""
    analyzed = (media.get("source") or {}).get(
        "analyzedRange", {"startMs": 0, "endMs": 0}
    )
    return {
        "schemaVersion": 1,
        "status": "failed",
        "sourceRevisionID": media.get("source", {}).get("revisionID", ""),
        "analysis": {
            "method": "ffprobe-frame-pts-and-ffmpeg-audio-envelope",
            "algorithmVersion": REVIEW_TIMELINE_VERSION,
            "analyzedRange": {
                "startMs": int(analyzed.get("startMs", 0)),
                "endMs": int(analyzed.get("endMs", 0)),
            },
        },
        "videoFrames": {
            "status": "unavailable",
            "timingMode": "unavailable",
            "reason": reason,
        },
        "waveform": {
            "status": "unavailable",
            "channelMode": "unavailable",
            "reason": reason,
        },
    }
