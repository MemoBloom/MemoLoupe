"""音频能量检测（docs/03 §2.9，schemas/audio-energy.json）。

ffmpeg 解码 mono s16le PCM，标准库 array + math 按固定窗算 RMS dB，
逐镜头聚合 median/min/max 与五档 label。无音轨与测得静音严格区分。

实现注记：run_process 统一捕获 stdout（项目外部进程纪律），PCM 字节只持有
一份，按窗增量聚合，不生成全量 Python 采样对象；s16le 为小端，x86/arm
均为小端，大端平台需 byteswap（此处显式检查）。
"""

from __future__ import annotations

import json
import math
import sys
from array import array
from pathlib import Path
from statistics import median

from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
from memoloupe.core.time_ranges import seconds_to_ms

AUDIO_ENERGY_VERSION = "energy.v1"

# 全零窗 RMS=0 的 log10 无定义；用地板值按静音处理（远低于 silent 阈值 -60）。
SILENCE_FLOOR_DB = -99.0

_FULL_SCALE = 32768.0


def rms_db(samples: array) -> float:
    """一窗 int16 采样的 RMS dB（20*log10(rms/32768)）；全零窗返回地板值。"""
    if not samples:
        return SILENCE_FLOOR_DB
    square_sum = sum(x * x for x in samples)
    rms = math.sqrt(square_sum / len(samples))
    if rms == 0.0:
        return SILENCE_FLOOR_DB
    return 20.0 * math.log10(rms / _FULL_SCALE)


def label_for_db(db: float, thresholds: dict) -> str:
    """五档 label；边界规则：小于号严格，等于阈值归较高档。"""
    if db < thresholds["silent"]:
        return "静音"
    if db < thresholds["low"]:
        return "低"
    if db < thresholds["medium"]:
        return "中"
    if db < thresholds["high"]:
        return "高"
    return "峰值"


def window_db_values(pcm: bytes, *, sample_rate: int, frame_ms: int) -> list[float]:
    """把 PCM 字节流按 frame_ms 窗切成 dB 序列；不足一窗的尾巴丢弃。"""
    window_samples = sample_rate * frame_ms // 1000
    window_bytes = window_samples * 2  # s16le
    values: list[float] = []
    for offset in range(0, len(pcm) - window_bytes + 1, window_bytes):
        window = array("h")
        window.frombytes(pcm[offset : offset + window_bytes])
        if sys.byteorder == "big":  # s16le 显式为小端
            window.byteswap()
        values.append(rms_db(window))
    return values


def assign_windows_to_shots(
    values: list[float], *, frame_ms: int, shots: list[dict]
) -> dict[str, list[float]]:
    """按窗起点时间把窗值归入 [finalStartMs, finalEndMs) 的镜头。"""
    assigned: dict[str, list[float]] = {shot["shotID"]: [] for shot in shots}
    ranges = [(s["shotID"], int(s["finalStartMs"]), int(s["finalEndMs"])) for s in shots]
    for index, db in enumerate(values):
        window_start_ms = index * frame_ms
        for shot_id, start_ms, end_ms in ranges:
            if start_ms <= window_start_ms < end_ms:
                assigned[shot_id].append(db)
                break
    return assigned


def aggregate_shot_windows(shot_id: str, values: list[float], thresholds: dict) -> dict:
    """单镜头聚合；frameCount=0 时不伪造任何数值。"""
    entry: dict = {
        "shotID": shot_id,
        "label": "unknown",
        "medianDb": None,
        "frameCount": len(values),
    }
    if values:
        median_db = median(values)
        entry["medianDb"] = median_db
        entry["minDb"] = min(values)
        entry["maxDb"] = max(values)
        entry["label"] = label_for_db(median_db, thresholds)
    return entry


def _probe_duration_ms(source: Path, config: dict, pool) -> int:
    result = pool.run(
        [
            config["ffmpeg"]["ffprobePath"],
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(source),
        ],
        timeout_sec=float(config["ffmpeg"]["probeTimeoutSec"]),
    )
    data = json.loads(result.stdout.decode("utf-8"))
    return seconds_to_ms(data["format"]["duration"])


def _empty_shots(shots: list[dict]) -> list[dict]:
    return [aggregate_shot_windows(shot["shotID"], [], {}) for shot in shots]


def detect_audio_energy(
    source: Path,
    shots: list[dict],
    has_audio: bool,
    config: dict,
    *,
    pool,
) -> dict:
    """检测每镜头音频能量，返回符合 audio-energy.json 的 dict。"""
    source = Path(source)
    energy_config = config["audioEnergy"]
    sample_rate = int(energy_config["sampleRate"])
    frame_ms = int(energy_config["frameMs"])
    thresholds = {k: float(v) for k, v in energy_config["thresholds"].items()}

    duration_ms = _probe_duration_ms(source, config, pool)

    if not has_audio:
        # schema 要求 sampleRate 为 >=1 的整数且不允许 null；无音轨时填本次
        # 分析配置的分析采样率（语义是"分析所用采样率"），由 hasAudio=false
        # 表达无音轨这一事实。
        result = {
            "source": str(source.expanduser().resolve()),
            "durationMs": duration_ms,
            "sampleRate": sample_rate,
            "hasAudio": False,
            "thresholds": thresholds,
            "shots": _empty_shots(shots),
        }
        validate_artifact(ArtifactName.AUDIO_ENERGY, result)
        return result

    result_proc = pool.run(
        [
            config["ffmpeg"]["ffmpegPath"],
            "-hide_banner", "-loglevel", "error",
            "-i", str(source),
            "-vn", "-f", "s16le", "-ac", "1", "-ar", str(sample_rate),
            "-",
        ],
        timeout_sec=float(config["ffmpeg"]["scanTimeoutSec"]),
    )
    values = window_db_values(result_proc.stdout, sample_rate=sample_rate, frame_ms=frame_ms)
    assigned = assign_windows_to_shots(values, frame_ms=frame_ms, shots=shots)

    result = {
        "source": str(source.expanduser().resolve()),
        "durationMs": duration_ms,
        "sampleRate": sample_rate,
        "hasAudio": True,
        "thresholds": thresholds,
        "shots": [
            aggregate_shot_windows(shot["shotID"], assigned[shot["shotID"]], thresholds)
            for shot in shots
        ],
    }
    validate_artifact(ArtifactName.AUDIO_ENERGY, result)
    return result
