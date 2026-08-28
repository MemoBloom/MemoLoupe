# 本地 ASR（FireRedVAD + MLX Whisper）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 MemoLoupe 增加本地 ASR provider `local-fireredvad-mlx`：FireRedVAD 做人声段切分，MLX whisper-large-v3-turbo 做识别，输出既有 `asr.json` 契约。

**Architecture:** 方案 A——新增 `services/asr_local.py` 实现现有 `ASRService` 协议，`build_asr_service` 按 `asr.provider` 分支构造；`asr_stage.py` / 指纹 / 降级矩阵 / schema 全部不变。流水线：ffmpeg 解 16kHz mono wav → FireRedVAD 出人声段 → 段合并、窗口打包、带静音分隔拼接 → 单次 `mlx_whisper.transcribe` → 拼接轴时间映射回原片毫秒。

**Tech Stack:** Python 3.12、numpy（已有依赖）、optional extra `asr-local`（`fireredvad`、`mlx-whisper>=0.4.3`）、ffmpeg（已有基础设施）、pytest。

**Spec:** `docs/superpowers/specs/2026-08-28-local-asr-fireredvad-mlx-design.md`

## Global Constraints

- 所有时间用整数毫秒；区间 `[startMs, endMs)`；秒→毫秒一律走 `memoloupe.core.time_ranges.seconds_to_ms`。
- 缺 `fireredvad` / `mlx_whisper` 依赖必须抛 `CapabilityUnavailableError`（阶段层落 `skipped`），不得 ImportError 泄出。
- 远程 provider（`openai-json` / `openai-multipart`）行为完全不变；`schemas/asr.json` 不改。
- 外部进程必须经 `memoloupe.media.proc.run_process`，argv 数组、不过 shell。
- 未知配置键不得静默吞掉：新配置项必须进 `DEFAULT_CONFIG`（env 覆盖依赖默认值存在）。
- VAD/whisper 明细只进 `rawExtras.local` 命名空间，不进主契约字段。
- 测试命令：`uv run pytest <path> -v`；全量回归 `uv run pytest -q`。
- 提交信息遵循仓库现有 conventional commits 风格。

---

### Task 1: 配置扩展（asr 组新增本地 provider 字段）

**Files:**
- Modify: `src/memoloupe/core/config.py`（`DEFAULT_CONFIG["asr"]`，约 88-96 行）
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `DEFAULT_CONFIG["asr"]` 新键——`language: None`、`localAsrVersion: "asr-local.v1"`、`vad: {modelDir, speechThreshold, smoothWindowSize, minSpeechFrame, maxSpeechFrame, minSilenceFrame}`、`whisper: {model, wordTimestamps}`、`mergeGapMs: 300`、`windowSec: 30`、`windowPadMs: 200`。Task 3 的 `LocalFireRedVadMlxASR` 消费整个 `asr` 组。

- [ ] **Step 1: 写失败测试**

在 `tests/unit/test_config.py` 追加：

```python
def test_asr_local_config_defaults_present():
    from memoloupe.core.config import DEFAULT_CONFIG
    asr = DEFAULT_CONFIG["asr"]
    assert asr["language"] is None
    assert asr["localAsrVersion"] == "asr-local.v1"
    assert asr["vad"]["speechThreshold"] == 0.4
    assert asr["vad"]["modelDir"] is None
    assert asr["whisper"]["model"] == "mlx-community/whisper-large-v3-turbo"
    assert asr["whisper"]["wordTimestamps"] is True
    assert asr["mergeGapMs"] == 300
    assert asr["windowSec"] == 30
    assert asr["windowPadMs"] == 200


def test_asr_local_env_override_nested():
    from memoloupe.core.config import load_config
    config = load_config(env={
        "MEMOLOUPE_ASR__PROVIDER": "local-fireredvad-mlx",
        "MEMOLOUPE_ASR__VAD__SPEECHTHRESHOLD": "0.5",
        "MEMOLOUPE_ASR__WHISPER__MODEL": "mlx-community/whisper-tiny",
        "MEMOLOUPE_ASR__MERGEGAPMS": "500",
    })
    assert config["asr"]["provider"] == "local-fireredvad-mlx"
    assert config["asr"]["vad"]["speechThreshold"] == 0.5
    assert config["asr"]["whisper"]["model"] == "mlx-community/whisper-tiny"
    assert config["asr"]["mergeGapMs"] == 500


def test_asr_fingerprint_changes_with_local_config():
    from memoloupe.core.config import config_fingerprint, load_config
    base = load_config(env={})
    changed = load_config(
        env={"MEMOLOUPE_ASR__WHISPER__MODEL": "mlx-community/whisper-tiny"}
    )
    assert config_fingerprint(base, ["asr"]) != config_fingerprint(changed, ["asr"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_config.py -k asr_local -v`
Expected: FAIL（`KeyError: 'language'` 等）

- [ ] **Step 3: 实现配置扩展**

`src/memoloupe/core/config.py` 的 `DEFAULT_CONFIG["asr"]` 替换为：

```python
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
```

- [ ] **Step 4: 跑测试确认通过 + 既有 config 测试不回归**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add src/memoloupe/core/config.py tests/unit/test_config.py
git commit -m "feat(config): asr local provider fields (vad/whisper/window)"
```

---

### Task 2: 纯函数——段合并、窗口打包、拼接轴时间映射

**Files:**
- Create: `src/memoloupe/services/asr_local.py`
- Test: `tests/unit/test_asr_local.py`

**Interfaces:**
- Consumes: `memoloupe.core.time_ranges.seconds_to_ms`。
- Produces（Task 3 依赖的精确签名）:
  - `PROVIDER_LOCAL = "local-fireredvad-mlx"`，`LOCAL_ASR_VERSION = "asr-local.v1"`，`SAMPLE_RATE = 16000`，`CONCAT_SILENCE_MS = 500`
  - `merge_vad_segments(timestamps: Iterable[tuple[float, float]], *, merge_gap_ms: int) -> list[tuple[int, int]]`
  - `pack_windows(segments: list[tuple[int, int]], *, window_ms: int, pad_ms: int, total_ms: int) -> list[tuple[int, int]]`
  - `build_concat_map(windows: list[tuple[int, int]], *, silence_ms: int) -> tuple[list[tuple[int, int, int]], int]`（entries 为 `(concat_start_ms, src_start_ms, src_dur_ms)`）
  - `concat_to_source(entries: list[tuple[int, int, int]], t_ms: int) -> int`
  - `map_concat_segments(whisper_segments, *, entries, range_start_ms: int) -> list[dict]`

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_asr_local.py`：

```python
"""services/asr_local 纯函数单元测试：段合并、窗口打包、拼接轴映射。"""

from __future__ import annotations

from memoloupe.services.asr_local import (
    build_concat_map,
    concat_to_source,
    map_concat_segments,
    merge_vad_segments,
    pack_windows,
)


def test_merge_vad_segments_merges_close_and_sorts():
    ts = [(2.0, 3.0), (0.5, 1.0), (1.2, 1.8), (5.0, 6.0)]
    # 排序后 (500,1000)/(1200,1800)/(2000,3000) 相邻间隔均 200ms <= 300ms，
    # 三段级联合并为 (500,3000)；(5000,6000) 间隔 2000ms 保持独立。
    assert merge_vad_segments(ts, merge_gap_ms=300) == [
        (500, 3000),
        (5000, 6000),
    ]


def test_merge_vad_segments_gap_beyond_threshold_stays_split():
    ts = [(0.5, 1.0), (1.5, 2.0)]
    assert merge_vad_segments(ts, merge_gap_ms=300) == [(500, 1000), (1500, 2000)]


def test_merge_vad_segments_drops_invalid():
    assert merge_vad_segments([(1.0, 1.0), (2.0, 1.0), (3.0, 4.0)],
                              merge_gap_ms=0) == [(3000, 4000)]


def test_pack_windows_splits_overlong_and_pads():
    segs = [(1000, 5000), (6000, 20000), (25000, 26000)]
    # window 上限 10s：第一段 + 第二段拼起来 19s 超限 → 拆窗
    windows = pack_windows(segs, window_ms=10_000, pad_ms=200, total_ms=30_000)
    assert windows == [(800, 5200), (5800, 20200), (24800, 26200)]


def test_pack_windows_clamps_to_total():
    windows = pack_windows([(0, 1000)], window_ms=10_000, pad_ms=200,
                           total_ms=1100)
    assert windows == [(0, 1100)]


def test_concat_map_roundtrip():
    windows = [(800, 5200), (5800, 20200)]
    entries, total = build_concat_map(windows, silence_ms=500)
    # 第一窗 4400ms + 静音 500ms + 第二窗 14400ms
    assert total == 4400 + 500 + 14400
    assert entries == [(0, 800, 4400), (4900, 5800, 14400)]
    # 拼接轴 1000ms → 源轴 1800ms
    assert concat_to_source(entries, 1000) == 1800
    # 落在静音区（4500ms）→ clamp 到第一窗末尾 5200ms
    assert concat_to_source(entries, 4500) == 5200
    # 第二窗起点
    assert concat_to_source(entries, 4900) == 5800


def test_map_concat_segments_offsets_and_filters():
    entries = [(0, 800, 4400)]
    whisper_segments = [
        {"start": 0.5, "end": 1.5, "text": " 你好 "},
        {"start": 2.0, "end": 2.0, "text": "无时长"},   # end<=start 丢弃
        {"start": 3.0, "end": 3.5, "text": "   "},       # 空文本丢弃
    ]
    out = map_concat_segments(whisper_segments, entries=entries,
                              range_start_ms=10_000)
    assert out == [
        {"startMs": 10_000 + 800 + 500, "endMs": 10_000 + 800 + 1500,
         "text": "你好", "speaker": None, "confidence": None},
    ]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_asr_local.py -v`
Expected: FAIL（`ModuleNotFoundError: memoloupe.services.asr_local`）

- [ ] **Step 3: 实现纯函数**

新建 `src/memoloupe/services/asr_local.py`：

```python
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

#: 本地实现版本（同时写入 DEFAULT_CONFIG["asr"]["localAsrVersion"] 进指纹）。
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_asr_local.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add src/memoloupe/services/asr_local.py tests/unit/test_asr_local.py
git commit -m "feat(asr): local pipeline pure helpers (merge/pack/concat-map)"
```

---

### Task 3: `LocalFireRedVadMlxASR` 服务类 + `build_asr_service` 分支

**Files:**
- Modify: `src/memoloupe/services/asr_local.py`（追加服务类与懒加载器）
- Modify: `src/memoloupe/services/asr.py`（`build_asr_service` 增分支，`__all__` 不变）
- Test: `tests/unit/test_asr_local.py`（追加）

**Interfaces:**
- Consumes: Task 2 全部纯函数；`memoloupe.media.proc.run_process`；`memoloupe.core.errors.CapabilityUnavailableError`；`memoloupe.services.asr.ASRRequest/ASRResult`。
- Produces:
  - `class LocalFireRedVadMlxASR(*, asr_config: dict, ffmpeg_path: str, decode_timeout_sec: float, decode_fn=None, vad_detect_fn=None, transcribe_fn=None)`，方法 `transcribe(media_path: Path, request: ASRRequest) -> ASRResult`
  - 注入钩子签名：`decode_fn(media_path, start_ms, end_ms, work_dir) -> tuple[Path, int]`（wav 路径 + 总毫秒）；`vad_detect_fn(wav_path) -> list[tuple[float, float]]`；`transcribe_fn(audio_np, language) -> dict`

- [ ] **Step 1: 写失败测试**

`tests/unit/test_asr_local.py` 追加：

```python
import wave
from pathlib import Path

import pytest

from memoloupe.core.errors import CapabilityUnavailableError
from memoloupe.services.asr import ASRRequest, build_asr_service
from memoloupe.services.asr_local import (
    LOCAL_ASR_VERSION,
    PROVIDER_LOCAL,
    LocalFireRedVadMlxASR,
)


def _write_wav(path: Path, total_ms: int = 10_000) -> Path:
    """写 16kHz mono s16le 静音 wav。"""
    frames = b"\x00\x00" * (total_ms * 16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(frames)
    return path


def _service(tmp_path: Path, **hooks) -> LocalFireRedVadMlxASR:
    cfg = {
        "vad": {"modelDir": None, "speechThreshold": 0.4, "smoothWindowSize": 5,
                "minSpeechFrame": 20, "maxSpeechFrame": 2000,
                "minSilenceFrame": 20},
        "whisper": {"model": "mlx-community/whisper-large-v3-turbo",
                    "wordTimestamps": True},
        "mergeGapMs": 300, "windowSec": 30, "windowPadMs": 200,
    }
    return LocalFireRedVadMlxASR(
        asr_config=cfg, ffmpeg_path="ffmpeg", decode_timeout_sec=60.0, **hooks
    )


def test_transcribe_full_flow_with_fakes(tmp_path: Path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fake")

    def fake_decode(media_path, start_ms, end_ms, work_dir):
        assert (start_ms, end_ms) == (1000, 11_000)
        return _write_wav(Path(work_dir) / "a.wav"), 10_000

    def fake_vad(wav_path):
        return [(0.5, 2.0), (2.2, 4.0)]  # 间隔 200ms 合并 → 一窗

    def fake_transcribe(audio, language):
        assert language == "zh"
        return {"segments": [{"start": 0.3, "end": 1.0, "text": "你好"}],
                "language": "zh"}

    service = _service(tmp_path, decode_fn=fake_decode,
                       vad_detect_fn=fake_vad, transcribe_fn=fake_transcribe)
    result = service.transcribe(media, ASRRequest(language="zh",
                                                  start_ms=1000, end_ms=11_000))
    # VAD 窗 [300, 2200]（pad 200）→ whisper 0.3s 映射回 300+300=600，
    # 再加 analyzedRange 起点 1000 → 1600
    assert [dict(s) for s in result.segments] == [
        {"startMs": 1600, "endMs": 2300, "text": "你好",
         "speaker": None, "confidence": None}
    ]
    assert result.raw_extras["local"]["provider"] == PROVIDER_LOCAL
    assert result.raw_extras["local"]["version"] == LOCAL_ASR_VERSION
    assert result.raw_extras["local"]["whisper"]["windowCount"] == 1


def test_transcribe_no_speech_returns_empty(tmp_path: Path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fake")
    service = _service(
        tmp_path,
        decode_fn=lambda *a: (_write_wav(Path(a[3]) / "a.wav"), 10_000),
        vad_detect_fn=lambda wav_path: [],
        transcribe_fn=lambda audio, language: pytest.fail("不应调用 whisper"),
    )
    result = service.transcribe(media, ASRRequest(start_ms=0, end_ms=10_000))
    assert result.segments == ()
    assert result.raw_extras["local"]["vad"]["segments"] == []


def test_missing_dependency_raises_capability_unavailable(tmp_path: Path):
    service = _service(tmp_path)  # 不注入钩子 → 真实懒加载路径
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fake")
    # CI 环境通常未装 fireredvad；若已装则本测试改为跳过
    pytest.importorskip  # noqa: B018 - 仅为说明意图
    try:
        import fireredvad  # noqa: F401
        import mlx_whisper  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("本地依赖已安装，无法验证缺依赖降级")
    with pytest.raises(CapabilityUnavailableError):
        service.transcribe(media, ASRRequest(start_ms=0, end_ms=None))


def test_build_asr_service_local_provider():
    config = {
        "asr": {"enabled": True, "provider": PROVIDER_LOCAL,
                "vad": {}, "whisper": {}, "mergeGapMs": 300,
                "windowSec": 30, "windowPadMs": 200},
        "ffmpeg": {"ffmpegPath": "ffmpeg", "scanTimeoutSec": 600.0},
    }
    service = build_asr_service(config)
    assert isinstance(service, LocalFireRedVadMlxASR)


def test_build_asr_service_local_disabled():
    config = {"asr": {"enabled": False, "provider": PROVIDER_LOCAL}}
    assert build_asr_service(config) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_asr_local.py -v`
Expected: FAIL（`LocalFireRedVadMlxASR` 不存在）

- [ ] **Step 3: 实现服务类与懒加载**

`src/memoloupe/services/asr_local.py` 追加：

```python
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
    root = snapshot_download(repo_id="FireRedTeam/FireRedVAD",
                             allow_patterns=["VAD/*"])
    return str(Path(root) / "VAD")


def _build_vad_detect(vad_cfg: dict):
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
        wav_path = Path(work_dir) / "asr-local-16k.wav"
        argv = [
            self._ffmpeg_path, "-hide_banner", "-loglevel", "error",
            "-ss", f"{start_ms / 1000:.3f}", "-i", str(media_path),
        ]
        if end_ms is not None:
            argv += ["-t", f"{(end_ms - start_ms) / 1000:.3f}"]
        argv += [
            "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-acodec", "pcm_s16le", "-f", "wav", "-y", str(wav_path),
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
                timestamps,
                merge_gap_ms=int(self._cfg.get("mergeGapMs", 300)),
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
                    "speechThreshold": float(
                        vad_cfg.get("speechThreshold", 0.4)
                    ),
                },
                "whisper": {
                    "model": str(
                        whisper_cfg.get("model") or DEFAULT_WHISPER_MODEL
                    ),
                    "windowCount": len(windows),
                },
            }
            if not windows:
                raw_local["note"] = "VAD 未检出人声段"
                return ASRResult(segments=(), raw_extras={"local": raw_local})

            samples = _wav_to_float32(wav_path)
            silence = np.zeros(
                CONCAT_SILENCE_MS * SAMPLES_PER_MS, dtype=np.float32
            )
            parts: list = []
            for index, (win_start, win_end) in enumerate(windows):
                if index > 0:
                    parts.append(silence)
                parts.append(
                    samples[win_start * SAMPLES_PER_MS:
                            win_end * SAMPLES_PER_MS]
                )
            concat = np.concatenate(parts)
            entries, concat_ms = build_concat_map(
                windows, silence_ms=CONCAT_SILENCE_MS
            )
            raw_local["whisper"]["concatMs"] = concat_ms

            transcribe_fn = self._transcribe_fn or _build_transcribe(
                whisper_cfg
            )
            result = transcribe_fn(concat, request.language)
            segments = map_concat_segments(
                result.get("segments", []),
                entries=entries,
                range_start_ms=request.start_ms,
            )
        return ASRResult(
            segments=tuple(segments), raw_extras={"local": raw_local}
        )
```

`src/memoloupe/services/asr.py` 的 `build_asr_service` 中，在
`provider = str(asr_cfg.get("provider", PROVIDER_JSON))` 之后、远程分支之前插入：

```python
    if provider == "local-fireredvad-mlx":
        # 本地 provider 无需 apiKey/baseUrl；依赖缺失在 transcribe 时抛
        # CapabilityUnavailableError，由阶段层落 skipped。
        from memoloupe.services.asr_local import LocalFireRedVadMlxASR

        ffmpeg_cfg = config.get("ffmpeg", {}) if isinstance(config, dict) else {}
        return LocalFireRedVadMlxASR(
            asr_config=asr_cfg,
            ffmpeg_path=str(ffmpeg_cfg.get("ffmpegPath", "ffmpeg")),
            decode_timeout_sec=float(ffmpeg_cfg.get("scanTimeoutSec", 600.0)),
        )
```

注意：现有 `if not (api_key and base_url and model): return None` 检查必须只对远程 provider 生效——把该检查移到本地分支之后（本地分支先 return，自然绕过）。

- [ ] **Step 4: 跑测试确认通过 + 既有 ASR 服务测试不回归**

Run: `uv run pytest tests/unit/test_asr_local.py tests/unit/test_services_asr.py tests/unit/test_asr_stage.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add src/memoloupe/services/asr_local.py src/memoloupe/services/asr.py tests/unit/test_asr_local.py
git commit -m "feat(asr): local FireRedVAD + MLX Whisper provider"
```

---

### Task 4: 依赖声明、文档、opt-in 真实测试与全量回归

**Files:**
- Modify: `pyproject.toml`（optional-dependencies）
- Modify: `.env.example`（本地 ASR 变量说明）
- Modify: `README.md`（安装段落补 `uv sync --extra asr-local`）
- Modify: `tests/integration/test_real_services_opt_in.py`（追加本地 ASR smoke）
- Modify: `docs/06_DECISIONS_AND_ASSUMPTIONS.md`（新增 D-045）
- Modify: `docs/08_DEVELOPMENT_ROADMAP.md`（§2 基线 + §10 下一步）
- Modify: `MemoLoupe-todolist.md`（Phase 05 区）

**Interfaces:**
- Consumes: Task 1-3 全部产物。

- [ ] **Step 1: pyproject 声明 optional extra**

`pyproject.toml` 在 `[dependency-groups]` 前插入：

```toml
[project.optional-dependencies]
asr-local = ["fireredvad", "mlx-whisper>=0.4.3"]
```

Run: `uv lock`（更新锁文件；不要求本环境安装 extra）

- [ ] **Step 2: opt-in 真实模型测试**

`tests/integration/test_real_services_opt_in.py` 追加：

```python
@pytest.mark.skipif(
    not REAL_ENABLED or not os.environ.get("MEMOLOUPE_TEST_MEDIA"),
    reason="本地 ASR 真实模型 smoke 未启用"
    "（MEMOLOUPE_RUN_REAL_SERVICE_TESTS=1 且 MEMOLOUPE_TEST_MEDIA=<视频路径>）",
)
def test_local_asr_real_models_smoke(tmp_path):
    """本地 FireRedVAD + MLX Whisper 全链路 smoke（需 asr-local extra）。"""
    fireredvad = pytest.importorskip("fireredvad")
    mlx_whisper = pytest.importorskip("mlx_whisper")
    from memoloupe.core.config import load_config
    from memoloupe.services.asr import ASRRequest, build_asr_service

    config = load_config(env={
        "MEMOLOUPE_ASR__PROVIDER": "local-fireredvad-mlx",
    })
    service = build_asr_service(config)
    result = service.transcribe(
        Path(os.environ["MEMOLOUPE_TEST_MEDIA"]), ASRRequest()
    )
    for seg in result.segments:
        assert seg["startMs"] < seg["endMs"]
        assert seg["text"].strip()
```

- [ ] **Step 3: 文档更新**

- `.env.example` 的 ASR 段后追加：

```text
# Local ASR (optional, provider=local-fireredvad-mlx): on-device
# FireRedVAD voice-activity segmentation + MLX Whisper recognition.
# Install first: uv sync --extra asr-local
# MEMOLOUPE_ASR__PROVIDER=local-fireredvad-mlx
# MEMOLOUPE_ASR__WHISPER__MODEL=mlx-community/whisper-large-v3-turbo
# MEMOLOUPE_ASR__VAD__MODELDIR=     # empty = auto-download from Hugging Face
# MEMOLOUPE_ASR__VAD__SPEECHTHRESHOLD=0.4
```

- `README.md` 安装/配置段落补一句：`uv sync --extra asr-local` 启用本地 ASR
  （FireRedVAD + MLX Whisper，`asr.provider=local-fireredvad-mlx`）。
- `docs/06_DECISIONS_AND_ASSUMPTIONS.md` 新增 D-045（注意文中已有重复编号的
  D-041/D-042，新条目用 D-045）：

```markdown
### D-045：本地 ASR provider（FireRedVAD + MLX Whisper）

- 新增 `asr.provider=local-fireredvad-mlx`：FireRedVAD 非流式 VAD 切人声段，
  段合并（mergeGapMs=300）→ ≤30s 窗口（pad 200ms）→ 窗口间插 500ms 静音
  拼接后单次 mlx-whisper（默认 mlx-community/whisper-large-v3-turbo）转写，
  拼接轴时间映射回原片毫秒。
- 依赖为 optional extra `asr-local`，lazy import；缺依赖抛
  CapabilityUnavailableError → asr.json status=skipped。
- `localAsrVersion` 进入 asr 配置指纹；VAD/whisper 明细只进
  rawExtras.local 命名空间。
- CALIBRATION：mergeGapMs=300、windowSec=30、windowPadMs=200、
  CONCAT_SILENCE_MS=500，待黄金视频校准（05-03）。
- 局限：whisper 段跨越拼接缝时端点分别映射，可能覆盖静音间隔；
  无说话人分离（speaker 恒为 null）。
```

- `docs/08_DEVELOPMENT_ROADMAP.md` §2 基线加一行"本地 ASR provider
  （FireRedVAD+MLX Whisper）已交付"；§10 的 05-01 条目中注明本地 ASR 可用。
- `MemoLoupe-todolist.md` Phase 05 区补一条 T5.2 子项：本地 ASR
  provider ✅（local-fireredvad-mlx）。

- [ ] **Step 4: 全量回归 + 样例校验**

Run: `uv run pytest -q`
Expected: 全 PASS（新增 opt-in 测试在无环境变量时 skip）

若有可用样例视频，再走一遍：
`uv run python run_shot_analysis.py <sample> <outdir>` + `uv run memoloupe validate <outdir> --strict`，
确认远程 provider 默认路径产物不变（asr 仍为 skipped 或走 mock）。

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock .env.example README.md \
  tests/integration/test_real_services_opt_in.py \
  docs/06_DECISIONS_AND_ASSUMPTIONS.md docs/08_DEVELOPMENT_ROADMAP.md \
  MemoLoupe-todolist.md
git commit -m "feat(asr): local provider deps, docs, opt-in real-model smoke"
```

---

## Self-Review 记录

- Spec 覆盖：配置（Task 1）、纯函数/架构（Task 2）、服务类与降级（Task 3）、
  依赖/安装/文档/测试（Task 4）均有对应任务；schema 不变符合 spec。
- 类型一致性：`decode_fn/vad_detect_fn/transcribe_fn` 签名在 Task 3 测试与
  实现中一致；`build_concat_map` 返回 `(entries, total)`，Task 2/3 用法一致。
- Task 3 的 `test_transcribe_full_flow_with_fakes` 期望值推算：VAD 段
  (0.5,2.0)+(2.2,4.0) 间隔 200ms ≤ mergeGapMs 300 → 合并为 [500,4000]；
  窗口 pad 200 → [300,4200]；whisper 段 (0.3,1.0)s 拼接轴 → 源轴
  300+300=600 / 300+1000=1300；加 range_start 1000 → 1600/2300。✓
