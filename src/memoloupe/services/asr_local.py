"""本地 ASR 适配器：FireRedVAD 人声切分 + MLX Whisper 识别（方案 A）。

流水线（全部在 ``transcribe`` 内完成，对外仍是 ASRService 协议）：

1. ffmpeg 把 analyzedRange 解码为 16kHz 16bit mono wav（临时文件）；
2. FireRedVAD 非流式检测人声段（秒级时间戳）；
3. 段合并（mergeGapMs）→ 贪心打包 ≤ windowSec 窗口（两端 windowPadMs）；
4. 窗口音频按序拼接（窗口间插入 CONCAT_SILENCE_MS 静音，防止跨段词汇粘连），
   单次 mlx_whisper.transcribe；
5. 拼接轴时间经 build_concat_map/concat_to_source 映射回原片毫秒。

依赖为 optional extra ``asr-local``（fireredvad / mlx-whisper）；lazy import，
缺依赖抛 CapabilityUnavailableError，由阶段层落 skipped 降级。
"""

from __future__ import annotations

from typing import Iterable

from memoloupe.core.time_ranges import seconds_to_ms

#: provider 取值（asr.provider）。
PROVIDER_LOCAL = "local-fireredvad-mlx"

#: 本地实现版本（与 DEFAULT_CONFIG["asr"]["localAsrVersion"] 保持一致）。
LOCAL_ASR_VERSION = "asr-local.v1"

#: 解码采样率（FireRedVAD 与 whisper 均要求 16kHz）。
SAMPLE_RATE = 16000

#: 窗口拼接时插入的静音间隔（CALIBRATION：防止跨段词汇粘连）。
CONCAT_SILENCE_MS = 500

#: 默认 whisper 模型（config["asr"]["whisper"]["model"] 可覆盖）。
DEFAULT_WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"

#: 每毫秒采样点数（16kHz mono）。
SAMPLES_PER_MS = SAMPLE_RATE // 1000


def merge_vad_segments(
    timestamps: Iterable[tuple[float, float]], *, merge_gap_ms: int
) -> list[tuple[int, int]]:
    """VAD 秒级时间戳 → 排序合并后的毫秒人声段；非法段（end<=start）丢弃。"""
    segs = sorted(
        (seconds_to_ms(float(s)), seconds_to_ms(float(e)))
        for s, e in timestamps
        if float(e) > float(s)
    )
    merged: list[list[int]] = []
    for start, end in segs:
        if merged and start - merged[-1][1] <= merge_gap_ms:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def pack_windows(
    segments: list[tuple[int, int]], *, window_ms: int, pad_ms: int, total_ms: int
) -> list[tuple[int, int]]:
    """贪心把人声段打包进 ≤ window_ms 的窗口，两端加 pad 并 clamp 到 [0, total_ms)。"""
    packed: list[list[int]] = []
    for start, end in segments:
        if packed and end - packed[-1][0] <= window_ms:
            packed[-1][1] = end
        else:
            packed.append([start, end])
    return [(max(0, s - pad_ms), min(total_ms, e + pad_ms)) for s, e in packed]


def build_concat_map(
    windows: list[tuple[int, int]], *, silence_ms: int
) -> tuple[list[tuple[int, int, int]], int]:
    """窗口（解码轴毫秒）→ 拼接轴映射。

    返回 ``(entries, total_ms)``；entries 每项为
    ``(concat_start_ms, src_start_ms, src_dur_ms)``，窗口间插入 silence_ms 静音。
    """
    entries: list[tuple[int, int, int]] = []
    concat = 0
    for index, (start, end) in enumerate(windows):
        if index > 0:
            concat += silence_ms
        entries.append((concat, start, end - start))
        concat += end - start
    return entries, concat


def concat_to_source(entries: list[tuple[int, int, int]], t_ms: int) -> int:
    """拼接轴毫秒 → 解码轴毫秒；落在静音区的点 clamp 到前一窗口末尾。"""
    if not entries:
        return 0
    for concat_start, src_start, dur in reversed(entries):
        if t_ms >= concat_start:
            return src_start + min(t_ms - concat_start, dur)
    return entries[0][1]


def map_concat_segments(
    whisper_segments, *, entries: list[tuple[int, int, int]], range_start_ms: int
) -> list[dict]:
    """whisper segments（秒，拼接轴）→ 归一化 segments（毫秒，原片时间轴）。"""
    out: list[dict] = []
    for seg in whisper_segments:
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        start = range_start_ms + concat_to_source(
            entries, seconds_to_ms(float(seg["start"]))
        )
        end = range_start_ms + concat_to_source(
            entries, seconds_to_ms(float(seg["end"]))
        )
        if end <= start:
            continue
        out.append(
            {
                "startMs": start,
                "endMs": end,
                "text": text,
                "speaker": None,
                "confidence": None,
            }
        )
    return out


import tempfile
import wave
from pathlib import Path

from memoloupe.core.errors import CapabilityUnavailableError
from memoloupe.media.proc import run_process
from memoloupe.services.asr import ASRRequest, ASRResult


def _import_fireredvad():
    try:
        from fireredvad import FireRedVad, FireRedVadConfig
    except ImportError:
        raise CapabilityUnavailableError(
            "asr-local", "缺少依赖 fireredvad（uv sync --extra asr-local）"
        ) from None
    return FireRedVad, FireRedVadConfig


def _import_mlx_whisper():
    try:
        import mlx_whisper
    except ImportError:
        raise CapabilityUnavailableError(
            "asr-local", "缺少依赖 mlx-whisper（uv sync --extra asr-local）"
        ) from None
    return mlx_whisper


def _resolve_vad_model_dir(vad_cfg: dict) -> str:
    model_dir = vad_cfg.get("modelDir")
    if model_dir:
        return str(Path(str(model_dir)).expanduser())
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        raise CapabilityUnavailableError(
            "asr-local", "缺少依赖 huggingface_hub（uv sync --extra asr-local）"
        ) from None
    root = snapshot_download(repo_id="FireRedTeam/FireRedVAD", allow_patterns=["VAD/*"])
    return str(Path(root) / "VAD")


def _build_vad_detect(vad_cfg: dict):
    """构造真实 FireRedVAD 检测函数（lazy import，模型仅加载一次）。"""
    FireRedVad, FireRedVadConfig = _import_fireredvad()
    vad = FireRedVad.from_pretrained(
        _resolve_vad_model_dir(vad_cfg),
        FireRedVadConfig(
            use_gpu=False,
            smooth_window_size=int(vad_cfg.get("smoothWindowSize", 5)),
            speech_threshold=float(vad_cfg.get("speechThreshold", 0.4)),
            min_speech_frame=int(vad_cfg.get("minSpeechFrame", 20)),
            max_speech_frame=int(vad_cfg.get("maxSpeechFrame", 2000)),
            min_silence_frame=int(vad_cfg.get("minSilenceFrame", 20)),
            merge_silence_frame=0,
            extend_speech_frame=0,
            chunk_max_frame=30000,
        ),
    )

    def detect(wav_path: Path) -> list[tuple[float, float]]:
        result, _probs = vad.detect(str(wav_path))
        return [(float(s), float(e)) for s, e in result.get("timestamps", [])]

    return detect


def _build_transcribe(whisper_cfg: dict):
    """构造真实 mlx-whisper 转写函数（lazy import）。"""
    mlx_whisper = _import_mlx_whisper()
    model = str(whisper_cfg.get("model") or DEFAULT_WHISPER_MODEL)
    word_ts = bool(whisper_cfg.get("wordTimestamps", True))

    def transcribe(audio, language: str | None) -> dict:
        kwargs = {
            "path_or_hf_repo": model,
            "word_timestamps": word_ts,
            "verbose": False,
        }
        if language:
            kwargs["language"] = language
        return mlx_whisper.transcribe(audio, **kwargs)

    return transcribe


def _wav_to_float32(wav_path: Path):
    import numpy as np

    with wave.open(str(wav_path), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


class LocalFireRedVadMlxASR:
    """本地 ASR：FireRedVAD 人声切分 + MLX Whisper 识别。

    ``decode_fn`` / ``vad_detect_fn`` / ``transcribe_fn`` 为可注入钩子
    （测试用）；None 时走真实 ffmpeg / fireredvad / mlx-whisper 懒加载。
    """

    def __init__(
        self,
        *,
        asr_config: dict,
        ffmpeg_path: str = "ffmpeg",
        decode_timeout_sec: float = 600.0,
        decode_fn=None,
        vad_detect_fn=None,
        transcribe_fn=None,
    ) -> None:
        self._cfg = asr_config
        self._ffmpeg_path = ffmpeg_path
        self._decode_timeout_sec = decode_timeout_sec
        self._decode_fn = decode_fn or self._decode_wav
        self._vad_detect_fn = vad_detect_fn
        self._transcribe_fn = transcribe_fn

    def _decode_wav(
        self,
        media_path: Path,
        start_ms: int,
        end_ms: int | None,
        work_dir: Path,
    ) -> tuple[Path, int]:
        """ffmpeg 解码 [start_ms, end_ms) 为 16kHz mono s16le wav，返回 (路径, 总毫秒)。"""
        wav_path = Path(work_dir) / "asr-local-16k.wav"
        argv = [
            self._ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start_ms / 1000:.3f}",
            "-i",
            str(media_path),
        ]
        if end_ms is not None:
            argv += ["-t", f"{(end_ms - start_ms) / 1000:.3f}"]
        argv += [
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-acodec",
            "pcm_s16le",
            "-f",
            "wav",
            "-y",
            str(wav_path),
        ]
        run_process(argv, timeout_sec=self._decode_timeout_sec)
        with wave.open(str(wav_path), "rb") as wf:
            total_ms = wf.getnframes() * 1000 // wf.getframerate()
        return wav_path, total_ms

    def transcribe(self, media_path: Path, request: ASRRequest) -> ASRResult:
        import numpy as np

        vad_cfg = self._cfg.get("vad", {})
        whisper_cfg = self._cfg.get("whisper", {})
        vad_detect = self._vad_detect_fn or _build_vad_detect(vad_cfg)

        with tempfile.TemporaryDirectory(prefix="memoloupe-asr-") as work_dir:
            wav_path, total_ms = self._decode_fn(
                media_path, request.start_ms, request.end_ms, Path(work_dir)
            )
            timestamps = vad_detect(wav_path)
            merged = merge_vad_segments(
                timestamps, merge_gap_ms=int(self._cfg.get("mergeGapMs", 300))
            )
            windows = pack_windows(
                merged,
                window_ms=int(self._cfg.get("windowSec", 30)) * 1000,
                pad_ms=int(self._cfg.get("windowPadMs", 200)),
                total_ms=total_ms,
            )
            raw_local: dict = {
                "provider": PROVIDER_LOCAL,
                "version": LOCAL_ASR_VERSION,
                "vad": {
                    "segments": [[s, e] for s, e in merged],
                    "speechThreshold": float(vad_cfg.get("speechThreshold", 0.4)),
                },
                "whisper": {
                    "model": str(whisper_cfg.get("model") or DEFAULT_WHISPER_MODEL),
                    "windowCount": len(windows),
                },
            }
            if not windows:
                raw_local["note"] = "VAD 未检出人声段"
                return ASRResult(segments=(), raw_extras={"local": raw_local})

            samples = _wav_to_float32(wav_path)
            silence = np.zeros(CONCAT_SILENCE_MS * SAMPLES_PER_MS, dtype=np.float32)
            parts: list = []
            for index, (win_start, win_end) in enumerate(windows):
                if index > 0:
                    parts.append(silence)
                parts.append(
                    samples[win_start * SAMPLES_PER_MS : win_end * SAMPLES_PER_MS]
                )
            concat = np.concatenate(parts)
            entries, concat_ms = build_concat_map(windows, silence_ms=CONCAT_SILENCE_MS)
            raw_local["whisper"]["concatMs"] = concat_ms

            transcribe_fn = self._transcribe_fn or _build_transcribe(whisper_cfg)
            result = transcribe_fn(concat, request.language)
            segments = map_concat_segments(
                result.get("segments", []),
                entries=entries,
                range_start_ms=request.start_ms,
            )
        return ASRResult(segments=tuple(segments), raw_extras={"local": raw_local})
