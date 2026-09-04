"""配置合并、脱敏快照与分阶段指纹（docs/01 §4.1）。

优先级：CLI > 环境变量（前缀 ``MEMOLOUPE_``，``__`` 分隔层级）>
配置文件（JSON）> 内置默认值。

密钥不得进入配置快照和指纹：``redacted_snapshot`` 把 key 名含
key/token/secret/password（大小写不敏感）的值替换为 ``"***"``，
``config_fingerprint`` 只指纹脱敏后的指定分组。
"""

from __future__ import annotations

import copy
import os
from pathlib import Path

from . import hashing
from .atomic_io import read_json
from .errors import ConfigError

ENV_PREFIX = "MEMOLOUPE_"
ENV_SEPARATOR = "__"
REDACTED = "***"
_SENSITIVE_SUBSTRINGS = ("key", "token", "secret", "password")

#: 保留给 connect 子系统自身的进程级环境变量，不参与配置树解析
#: （否则 load_config 会把它们当未知配置项拒绝）。
RESERVED_ENV_KEYS = frozenset(
    {"MEMOLOUPE_CONNECTIONS_PATH", "MEMOLOUPE_SECRET_STORE"}
)

# 内置默认值按 docs/01 §4.1 分组；数值取自 docs/03 的协议默认参数。
DEFAULT_CONFIG: dict = {
    "runtime": {
        "outputDir": "output",
        "logLevel": "INFO",
        "resume": True,
        "lockFileName": ".memoloupe.lock",
    },
    "ffmpeg": {
        "ffmpegPath": "ffmpeg",
        "ffprobePath": "ffprobe",
        "globalConcurrency": 2,
        "probeTimeoutSec": 30.0,
        "scanTimeoutSec": 300.0,
        "clipTimeoutSec": 120.0,
        "frameTimeoutSec": 30.0,
    },
    "shots": {
        "histogramBins": 254,
        "analysisSize": 128,
        "minimumFrames": 8,
        "fullFrameRate": True,
        "maxAnalysisFps": 60.0,
        "minimumShotMs": 500,
        "rapidCutMinimumMs": 200,
        "adaptiveWindow": 3,
        "adaptiveThreshold": 3.5,
        "minContentValue": 15.0,
        "hardCutThreshold": 60.0,
        "ssimMaxForAdaptive": 0.94,
        "edgeWeight": 0.25,
        "analysisFps": 2.0,
    },
    "audioCuts": {
        "analysisSampleRate": 16000,
        "frameMs": 20,
        "threshold": 8.0,
        "syncToleranceMs": 100,
        "associationWindowMs": 500,
    },
    "music": {
        "enabled": True,
        "sampleRate": 22050,
        "musicLevelDb": -18.0,
        "musicBassEnergy": 150.0,
        # 只有接近数字静音的持续区间才可形成确定性 absent；安静的歌词间隙
        # 不能被当作“没有 BGM”。
        "silentLevelDb": -55.0,
        # 全轨扫描的高精度音乐条件。响度本身不构成音乐证据，必须同时满足
        # 低频/调性与谱平坦度约束，避免把对白或宽带噪声判成音乐。
        "musicFlatnessMax": 0.50,
        "musicTonalFlatnessMax": 0.35,
        "musicMinimumBassEnergy": 50.0,
        # 滑动窗状态先合并短缺口，再移除短促命中；防止鼓点/瞬态噪声形成
        # 独立 BGM 区间。
        "musicMinimumRunMs": 400,
        "musicMergeGapMs": 600,
        "silentMinimumRunMs": 500,
    },
    "quality": {
        "videoSampleFps": 2.0,
        "blurFlagThreshold": 11.0,
        "underexposedYAVG": 40.0,
        "overexposedYAVG": 215.0,
    },
    #: 运动复刻候选检测（docs/03 §2.6，Phase 05-07）。目前只有 sampleFps
    #: 影响行为；宽高/平移上限等由算法常量固定并在产物 analysis 中自证。
    "motionEffects": {
        "sampleFps": 8.0,
    },
    #: Phase 06 审片工作台：确定性帧索引与波形 envelope 参数。
    #: 波形只存归一化 min/max envelope；bin 数超上限时自适应增大 bin 时长。
    "reviewTimeline": {
        "waveformSampleRate": 16000,
        "waveformBinMs": 20,
        "maxWaveformBins": 24000,
        "waveformChunkSec": 120,
        "framePtsTimeoutSec": 120.0,
        "waveformTimeoutSec": 120.0,
        "transitionFrameWidth": 320,
    },
    "audioEnergy": {
        "sampleRate": 16000,
        "frameMs": 20,
        "thresholds": {
            "silent": -60.0,
            "low": -40.0,
            "medium": -25.0,
            "high": -12.0,
        },
    },
    "vision": {
        "sampleFps": 2.0,
        "maximumFramesPerShot": 12,
        "maximumImageDimension": 960,
    },
    "asr": {
        "enabled": True,
        "provider": "openai-json",
        "baseUrl": None,
        "apiKey": None,
        "model": None,
        "fileField": "file",
        "timeoutSec": 120.0,
        # None = 自动检测语言；本地与远程 provider 共用。
        "language": None,
        # 本地 provider（local-fireredvad-mlx）实现版本；bump 即失效旧缓存。
        "localAsrVersion": "asr-local.v1",
        "vad": {
            # None = 首次运行从 HF 自动下载 FireRedTeam/FireRedVAD 的 VAD/ 子目录。
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
        # CALIBRATION：相邻人声段合并阈值。
        "mergeGapMs": 300,
        # 单个 whisper 转写窗口上限（秒）。
        "windowSec": 30,
        # 窗口两端 padding（毫秒）。
        "windowPadMs": 200,
    },
    "unifiedModel": {
        "baseUrl": None,
        "apiKey": None,
        "model": None,
        "fallbackModel": None,
        "timeoutSec": 300.0,
        "batchSize": 4,
        "concurrency": 10,
        "videoFPS": 10.0,
        "mediaResolution": "default",
        # 结构化镜头提取不需要长链推理；限制输出并关闭 MiMo 深度思考，
        # 避免默认 32K 输出上限导致多镜头请求长时间占用连接。
        "maxCompletionTokens": 4096,
        "thinkingMode": "disabled",
        "maxRetries": 3,
        "transport": "mediaDataURI",
    },
    "textModel": {
        "baseUrl": None,
        "apiKey": None,
        "model": None,
        "timeoutSec": 300.0,
        # 0 表示使用服务端默认上限；正整数会作为默认 max_tokens 传给请求。
        "maxTokens": 0,
    },
    "story": {
        # CALIBRATION A-006：1200 → 2000（2026-09-03，disney.MP4 证据：
        # ≤2s 的演唱间隙是段落内换气，不是叙事边界）。
        "gapMs": 2000,
        "boundarySource": "asr-gap",
    },
    "profile": {
        "enabled": True,
    },
    "render": {
        "language": "zh",
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    """递归合并两个 dict，返回新 dict；override 中的非 dict 值覆盖 base。"""
    if not isinstance(override, dict):
        raise ConfigError(f"配置覆盖必须是 JSON 对象: {type(override).__name__}")
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(s in lowered for s in _SENSITIVE_SUBSTRINGS)


def redacted_snapshot(config: dict) -> dict:
    """递归脱敏：key 名含 key/token/secret/password 的值替换为 ``"***"``。"""
    snapshot: dict = {}
    for key, value in config.items():
        if _is_sensitive_key(key):
            snapshot[key] = REDACTED
        elif isinstance(value, dict):
            snapshot[key] = redacted_snapshot(value)
        elif isinstance(value, list):
            snapshot[key] = [
                redacted_snapshot(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            snapshot[key] = copy.deepcopy(value)
    return snapshot


def config_fingerprint(config: dict, groups: list[str]) -> str:
    """只对指定分组的脱敏内容生成指纹（密钥变化不影响指纹）。"""
    snapshot = redacted_snapshot(config)
    parts = {group: snapshot.get(group) for group in groups}
    return hashing.fingerprint(parts)


def _coerce_env_value(raw: str, default: object, env_key: str) -> object:
    """按默认值类型把环境变量字符串转为对应类型。"""
    if isinstance(default, bool):
        lowered = raw.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"环境变量 {env_key} 的布尔值非法: {raw!r}")
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"环境变量 {env_key} 的整数值非法: {raw!r}") from exc
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"环境变量 {env_key} 的浮点值非法: {raw!r}") from exc
    return raw


def _env_overrides(env: dict[str, str]) -> dict:
    """把 ``MEMOLOUPE_SHOTS__MINIMUMFRAMES=8`` 形式解析为嵌套 dict。

    分组与键名大小写不敏感地匹配 DEFAULT_CONFIG；未知分组/键抛 ConfigError。
    """
    overrides: dict = {}
    for env_key, raw in env.items():
        if not env_key.startswith(ENV_PREFIX) or env_key in RESERVED_ENV_KEYS:
            continue
        body = env_key[len(ENV_PREFIX) :]
        segments = [s for s in body.split(ENV_SEPARATOR) if s]
        if not segments:
            continue
        node: dict = overrides
        default_node: object = DEFAULT_CONFIG
        for i, segment in enumerate(segments):
            if not isinstance(default_node, dict):
                raise ConfigError(f"环境变量 {env_key} 层级过深")
            match = next(
                (k for k in default_node if k.lower() == segment.lower()), None
            )
            if match is None:
                raise ConfigError(f"环境变量 {env_key} 引用了未知配置项: {segment!r}")
            if i == len(segments) - 1:
                node[match] = _coerce_env_value(raw, default_node[match], env_key)
            else:
                node = node.setdefault(match, {})
                default_node = default_node[match]
    return overrides


def load_config(
    cli_overrides: dict | None = None,
    env: dict[str, str] | None = None,
    config_file: Path | None = None,
) -> dict:
    """按 CLI > 环境变量 > 配置文件 > 默认值 的优先级合并出有效配置。"""
    if env is None:
        env = dict(os.environ)
    config = copy.deepcopy(DEFAULT_CONFIG)
    if config_file is not None:
        config = deep_merge(config, read_json(Path(config_file)))
    config = deep_merge(config, _env_overrides(env))
    if cli_overrides:
        config = deep_merge(config, cli_overrides)
    return config


def load_env_file(path: Path) -> dict[str, str]:
    """解析 ``.env`` 文件为环境变量 dict（05-05）。

    - 空行与 ``#`` 注释忽略；``KEY=VALUE`` 取值去引号；
    - **不覆盖进程已有的环境变量**（键已存在于 ``os.environ`` 时跳过）；
    - 文件不存在或不可读时返回空 dict（行为可预测）。
    """
    result: dict[str, str] = {}
    path = Path(path)
    if not path.is_file():
        return result
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if not key:
            continue
        if key in os.environ:
            continue  # 不覆盖已有环境变量
        result[key] = value
    return result
