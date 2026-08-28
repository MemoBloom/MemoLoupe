# 本地 ASR（FireRedVAD + MLX Whisper）设计

日期：2026-08-28
状态：已获用户确认（方案 A）
关联：roadmap Phase 05-01（真实服务联调的本地化分支）、docs/03 §2.7、docs/01 §7.1

## 背景与目标

当前 ASR 仅有 OpenAI 兼容远程适配器（openai-json / openai-multipart），未配置端点时
`asr.json` 恒为 `skipped`。目标：提供一条完全本地化的 ASR 链路——

- **FireRedVAD**（`FireRedTeam/FireRedVAD`，PyPI `fireredvad`）做人声段检测（VAD），
  只把有人声的片段送识别，减少幻觉并加速；
- **MLX Whisper**（`mlx-whisper`，模型 `mlx-community/whisper-large-v3-turbo`）
  在 Apple Silicon 上做语音识别。

## 已定决策（来自需求澄清）

1. FireRedVAD 角色：**只做 VAD 切分喂 Whisper**，不做人声分离/降噪（FireRedVAD 本身
   不含分离网络；分离留作未来可插拔阶段）。
2. 依赖形式：**主 venv optional extra**（`uv sync --extra asr-local`），代码内 lazy
   import，缺依赖走既有 `skipped` 降级。不做独立子进程 helper。
3. 集成形态：**方案 A**——新增本地 provider，复用现有 `ASRService` 端口，
   `asr_stage.py` / 指纹 / 降级矩阵 / `schemas/asr.json` 全部不变。

## 架构

```
run_asr_stage（不变）
  └─ build_asr_service(config)          # services/asr.py 新增 provider 分支
       └─ LocalFireRedVadMlxASR          # services/asr_local.py（新文件）
            ├─ 1. ffmpeg 解码 analyzedRange → 16kHz mono s16le 临时 wav
            │     （复用 config["ffmpeg"]，进程走 media/proc.run_process）
            ├─ 2. FireRedVad.detect(wav) → timestamps [(秒, 秒)]
            ├─ 3. 段合并 + 窗口打包（纯函数）
            │     相邻段间隔 ≤ mergeGapMs 合并；贪心打包进 ≤ windowSec 窗口
            ├─ 4. 逐窗 mlx_whisper.transcribe(..., word_timestamps=True)
            └─ 5. 秒→ms + 窗口偏移映射回原片时间轴 → ASRResult
```

## 配置（`core/config.py` 的 `asr` 组扩展）

```python
"asr": {
    "enabled": True,
    "provider": "openai-json",        # 新增取值 "local-fireredvad-mlx"
    # ... 现有远程字段不动 ...
    "language": None,                  # 已有；None=自动检测
    # 本地 provider 专用：
    "vad": {
        "modelDir": None,              # None=从 HF 自动下载到缓存目录
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
    "mergeGapMs": 300,                 # CALIBRATION：相邻人声段合并阈值
    "windowSec": 30,                   # Whisper 单次转写窗口上限
    "windowPadMs": 200,                # 窗口两端 padding
}
```

指纹：`config_fingerprint(config, ["asr"])` 已覆盖整组，配置/模型变化自动失效缓存；
实现版本常量 `LOCAL_ASR_VERSION` 以 `"localAsrVersion"` 键放入 `asr` 配置组默认值，
随配置指纹自然生效（本地实现变更时 bump 该常量即失效旧缓存）。

## 关键实现点

- **音频解码**：ffmpeg `-ar 16000 -ac 1 -acodec pcm_s16le -f wav`，限定
  analyzedRange；临时 wav 写入 output-dir 的 runtime 临时区，finally 清理。
- **段合并/窗口打包/偏移映射**：纯函数（`merge_segments` / `pack_windows` /
  `shift_to_source`），毫秒整数运算，便于单测。
- **Whisper 输入**：每窗口从 16kHz PCM 切片（numpy）直接喂 `mlx_whisper.transcribe`
  （支持 np.ndarray），避免逐窗写文件。
- **时间映射**：whisper 返回的 segment start/end（秒）→ `seconds_to_ms` →
  加窗口起点偏移（含 analyzedRange.startMs）得到原片时间轴毫秒。
- **rawExtras**：`{"local": {"vad": {"timestamps": ..., "config": ...},
  "whisper": {"model": ..., "windowCount": n}}}`，命名空间隔离，不进主契约。

## 错误与降级

| 情况 | 结果 |
|---|---|
| 无音轨 / provider 未配置 | 现有 `skipped` 语义不变 |
| `fireredvad` / `mlx_whisper` 导入失败 | `CapabilityUnavailableError` → `skipped` + note |
| 模型下载 / 推理异常 | `failed` + 脱敏诊断（`asr_stage.py` 已有，不改） |
| VAD 检测无人声 | `complete` + 空 segments + note |
| 远程 provider | 完全不受影响 |

## 依赖与安装

- `pyproject.toml` 新增：
  ```toml
  [project.optional-dependencies]
  asr-local = ["fireredvad", "mlx-whisper>=0.4.3"]
  ```
- 首次运行自动下载模型：FireRedVAD 经 `huggingface_hub.snapshot_download`
  （`FireRedTeam/FireRedVAD`，仅取 `VAD/` 子目录，约 2.2MB）；whisper 模型由
  mlx-whisper 自动从 HF 拉取。
- `.env.example` 与 README 补 `uv sync --extra asr-local` 安装说明。

## 测试策略

- 单元（`tests/unit/test_asr_local.py`）：
  - 段合并 / 窗口打包 / 时间偏移映射（纯函数，构造合成时间戳）；
  - 秒→ms 归一化与 analyzedRange 偏移；
  - provider 构造分支（`build_asr_service` 选 `local-fireredvad-mlx`）；
  - 缺依赖降级（模拟 ImportError → skipped，不抛）；
  - VAD 无人声 → complete + 空 segments。
- 契约：`schemas/asr.json` 不变，现有契约测试不动。
- opt-in 真实模型测试：`MEMOLOUPE_RUN_REAL_SERVICE_TESTS=1` 下用小型样例 wav
  跑通 VAD+Whisper 全链路（默认 skip，不进无凭据 CI）。
- 既有 MockASRService / CLI 路径回归：全量 `uv run pytest -q`。

## 文档更新

- `docs/06_DECISIONS_AND_ASSUMPTIONS.md`：新增决策（本地 ASR provider、
  mergeGapMs/windowSec 默认值列入 CALIBRATION）。
- `docs/08_DEVELOPMENT_ROADMAP.md` §2/§10：登记本地 ASR 能力。
- `MemoLoupe-todolist.md`：Phase 05 区补勾本地 ASR 条目。
- `AGENTS.md` 若涉及结构/命令变化则同步。

## 明确不做（YAGNI）

- 人声分离/降噪阶段（FireRedVAD 无此能力，未来另立可插拔阶段）。
- VAD 独立产物 `vad.json`（方案 B；VAD 分段暂存 rawExtras，需要时再升级）。
- streaming VAD / AED（唱歌/音乐事件检测）——本期只用非流式 VAD。
- 说话人分离（diarization）：segments 的 `speaker` 维持 None。
