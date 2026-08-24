"""质量检测（docs/03 §2.10，schemas/quality-flags.json）。

单次 ffmpeg 扫描：`fps=N,blurdetect,signalstats,blackdetect,freezedetect`
加 `metadata=mode=print:file=-`，从 stdout 解析机器键（lavfi.blur、
lavfi.signalstats.YAVG），blackdetect/freezedetect 事件从 stderr 日志解析
（两者均为 locale 无关的固定格式）。音频削波用 astats=metadata=1 的逐帧
Peak level（dB）判定。解析器不依赖任何本地化文案。
"""

from __future__ import annotations

import re
from pathlib import Path
from statistics import median

from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
from memoloupe.core.time_ranges import seconds_to_ms
from memoloupe.media.proc import ProcessError

QUALITY_DETECTION_VERSION = "quality.v1"

METHOD = "ffmpeg blurdetect + signalstats + astats + blackdetect + freezedetect"

# 音频削波判定峰值（dB，CALIBRATION）：逐帧 Peak level >= 该值判削波
AUDIO_CLIP_PEAK_DB = -0.1
# 视频采样 2fps（间隔 0.5s）下 blackdetect 的最短黑场，与采样粒度匹配
BLACK_MIN_DURATION_SEC = 0.4
# 采样数不足时 confidence=unknown 的分界
MIN_VIDEO_SAMPLES = 2

_FLAG_ORDER = ("画面模糊", "欠曝", "过曝", "音频削波", "黑场", "画面冻结")

_FRAME_HEADER_RE = re.compile(r"^frame:\d+\s+pts:\S+\s+pts_time:(-?[\d.eE+]+)")
_METADATA_RE = re.compile(r"^(lavfi\.[\w.]+)=(\S+)")
_BLACK_RE = re.compile(r"black_start:(\S+)\s+black_end:(\S+)\s+black_duration:\S+")
_FREEZE_RE = re.compile(r"lavfi\.freezedetect\.(freeze_start|freeze_end|freeze_duration):\s*(\S+)")


def _parse_metadata_frames(text: str) -> list[tuple[float, dict[str, str]]]:
    """解析 metadata/ametadata print 输出为 [(pts_time 秒, {键: 值})]。"""
    frames: list[tuple[float, dict[str, str]]] = []
    current: dict[str, str] | None = None
    current_pts = 0.0
    for line in text.splitlines():
        line = line.strip()
        header = _FRAME_HEADER_RE.match(line)
        if header:
            if current is not None:
                frames.append((current_pts, current))
            current_pts = float(header.group(1))
            current = {}
            continue
        if current is None:
            continue
        entry = _METADATA_RE.match(line)
        if entry:
            current[entry.group(1)] = entry.group(2)
    if current is not None:
        frames.append((current_pts, current))
    return frames


def parse_video_samples(text: str) -> list[dict]:
    """从 metadata print 解析视频样本；``nan`` 的 blur 按缺失处理。"""
    samples: list[dict] = []
    for pts_sec, keys in _parse_metadata_frames(text):
        blur_raw = keys.get("lavfi.blur")
        yavg_raw = keys.get("lavfi.signalstats.YAVG")
        samples.append(
            {
                "timeMs": seconds_to_ms(pts_sec),
                "blur": None
                if blur_raw is None or blur_raw.lower() == "nan"
                else float(blur_raw),
                "yavg": None if yavg_raw is None else float(yavg_raw),
            }
        )
    return samples


def parse_blackdetect_events(stderr: str) -> list[tuple[int, int]]:
    """解析 ``black_start:… black_end:…`` 日志行为毫秒区间。"""
    return [
        (seconds_to_ms(m.group(1)), seconds_to_ms(m.group(2)))
        for m in _BLACK_RE.finditer(stderr)
    ]


def parse_freezedetect_events(stderr: str) -> list[tuple[int, int | None]]:
    """解析 freeze_start/freeze_end 日志行并配对；EOF 未闭合的 freeze endMs=None。"""
    starts: list[int] = []
    ends: list[int] = []
    for m in _FREEZE_RE.finditer(stderr):
        if m.group(1) == "freeze_start":
            starts.append(seconds_to_ms(m.group(2)))
        elif m.group(1) == "freeze_end":
            ends.append(seconds_to_ms(m.group(2)))
    events: list[tuple[int, int | None]] = []
    for index, start in enumerate(starts):
        end = ends[index] if index < len(ends) else None
        if end is not None and end <= start:
            continue  # 配对异常，丢弃
        events.append((start, end))
    return events


def parse_audio_peaks(text: str) -> list[tuple[int, float]]:
    """解析 ametadata 逐帧 Peak level（dB）为 [(timeMs, peakDb)]。"""
    peaks: list[tuple[int, float]] = []
    for pts_sec, keys in _parse_metadata_frames(text):
        raw = keys.get("lavfi.astats.Overall.Peak_level")
        if raw is not None:
            peaks.append((seconds_to_ms(pts_sec), float(raw)))
    return peaks


def _overlaps_shot(event_start: int, event_end: int | None, shot_start: int, shot_end: int) -> bool:
    effective_end = event_end if event_end is not None else 2**62
    return event_start < shot_end and shot_start < effective_end


def build_shot_entry(
    shot: dict,
    *,
    video_samples: list[dict],
    audio_peaks: list[tuple[int, float]],
    black_events: list[tuple[int, int]],
    freeze_events: list[tuple[int, int | None]],
    has_audio: bool,
    thresholds: dict,
) -> dict:
    """聚合单镜头 flags/confidence/measurements；无样本不伪造数值。"""
    start_ms = int(shot["finalStartMs"])
    end_ms = int(shot["finalEndMs"])

    blurs = [s["blur"] for s in video_samples if s["blur"] is not None]
    yavgs = [s["yavg"] for s in video_samples if s["yavg"] is not None]
    peaks = [p for t, p in audio_peaks if start_ms <= t < end_ms]

    measurements: dict[str, float] = {"videoSampleCount": len(video_samples)}
    if has_audio:
        measurements["audioSampleCount"] = len(peaks)
    median_blur = median(blurs) if blurs else None
    median_yavg = median(yavgs) if yavgs else None
    if median_blur is not None:
        measurements["medianBlur"] = median_blur
    if median_yavg is not None:
        measurements["medianYAVG"] = median_yavg
    if has_audio and peaks:
        measurements["audioPeakDb"] = max(peaks)

    triggered: set[str] = set()
    if median_blur is not None and median_blur >= thresholds["blurFlagThreshold"]:
        triggered.add("画面模糊")
    if median_yavg is not None and median_yavg <= thresholds["underexposedYAVG"]:
        triggered.add("欠曝")
    if median_yavg is not None and median_yavg >= thresholds["overexposedYAVG"]:
        triggered.add("过曝")
    # 无音轨时绝不报音频削波（audioStatus=absent 是显式状态）
    if has_audio and any(p >= thresholds["audioClipPeakDb"] for p in peaks):
        triggered.add("音频削波")
    if any(_overlaps_shot(s, e, start_ms, end_ms) for s, e in black_events):
        triggered.add("黑场")
    if any(_overlaps_shot(s, e, start_ms, end_ms) for s, e in freeze_events):
        triggered.add("画面冻结")

    return {
        "shotID": shot["shotID"],
        "startMs": start_ms,
        "endMs": end_ms,
        "flags": [flag for flag in _FLAG_ORDER if flag in triggered],
        "confidence": "high"
        if len(video_samples) >= MIN_VIDEO_SAMPLES
        else "unknown",
        "measurements": measurements,
    }


def _assign_video_samples(samples: list[dict], shots: list[dict]) -> dict[str, list[dict]]:
    """按 timeMs ∈ [startMs, endMs) 归镜头；超出末镜头终点的样本归入末镜头。"""
    assigned: dict[str, list[dict]] = {s["shotID"]: [] for s in shots}
    if not shots:
        return assigned
    ranges = [(s["shotID"], int(s["finalStartMs"]), int(s["finalEndMs"])) for s in shots]
    last_id, _, last_end = ranges[-1]
    for sample in samples:
        t = sample["timeMs"]
        for shot_id, start_ms, end_ms in ranges:
            if start_ms <= t < end_ms:
                assigned[shot_id].append(sample)
                break
        else:
            if t >= last_end:
                assigned[last_id].append(sample)
    return assigned


def detect_quality(
    source: Path,
    shots: list[dict],
    has_audio: bool,
    config: dict,
    *,
    pool,
) -> dict:
    """扫描源文件并逐镜头产出质量 flags，返回符合 quality-flags.json 的 dict。"""
    source = Path(source)
    ffmpeg = config["ffmpeg"]["ffmpegPath"]
    scan_timeout = float(config["ffmpeg"]["scanTimeoutSec"])
    quality_config = config["quality"]
    sample_fps = float(quality_config["videoSampleFps"])
    thresholds = {
        "videoSampleFps": sample_fps,
        "blurFlagThreshold": float(quality_config["blurFlagThreshold"]),
        "underexposedYAVG": float(quality_config["underexposedYAVG"]),
        "overexposedYAVG": float(quality_config["overexposedYAVG"]),
        "audioClipPeakDb": AUDIO_CLIP_PEAK_DB,
    }

    video_chain = (
        f"fps={sample_fps:g},blurdetect,signalstats,"
        f"blackdetect=d={BLACK_MIN_DURATION_SEC:g},freezedetect,"
        "metadata=mode=print:file=-"
    )
    video_result = pool.run(
        [
            ffmpeg, "-hide_banner",
            "-i", str(source),
            "-vf", video_chain,
            "-an", "-f", "null", "-",
        ],
        timeout_sec=scan_timeout,
    )
    video_text = video_result.stdout.decode("utf-8", errors="replace")
    stderr_text = video_result.stderr.decode("utf-8", errors="replace")
    samples = parse_video_samples(video_text)
    black_events = parse_blackdetect_events(stderr_text)
    freeze_events = parse_freezedetect_events(stderr_text)

    audio_status = "absent"
    audio_peaks: list[tuple[int, float]] = []
    if has_audio:
        try:
            audio_result = pool.run(
                [
                    ffmpeg, "-hide_banner",
                    "-i", str(source),
                    "-vn",
                    "-af",
                    "astats=metadata=1,"
                    "ametadata=mode=print:key=lavfi.astats.Overall.Peak_level:file=-",
                    "-f", "null", "-",
                ],
                timeout_sec=scan_timeout,
            )
            audio_peaks = parse_audio_peaks(
                audio_result.stdout.decode("utf-8", errors="replace")
            )
            audio_status = "complete"
        except ProcessError:
            # 音频扫描失败：显式 failed，不产出任何音频削波结论
            audio_status = "failed"

    assigned = _assign_video_samples(samples, shots)
    shot_entries = [
        build_shot_entry(
            shot,
            video_samples=assigned[shot["shotID"]],
            audio_peaks=audio_peaks,
            black_events=black_events,
            freeze_events=freeze_events,
            has_audio=has_audio and audio_status == "complete",
            thresholds=thresholds,
        )
        for shot in shots
    ]

    result = {
        "status": "complete",
        "version": QUALITY_DETECTION_VERSION,
        "method": METHOD,
        "audioStatus": audio_status,
        "flaggedShotCount": sum(1 for s in shot_entries if s["flags"]),
        "shotCount": len(shot_entries),
        "thresholds": thresholds,
        "shots": shot_entries,
    }
    validate_artifact(ArtifactName.QUALITY_FLAGS, result)
    return result
