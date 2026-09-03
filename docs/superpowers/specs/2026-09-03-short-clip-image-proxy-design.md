# 短镜头模型代理改用图像输入 设计

日期：2026-09-03
状态：已批准（brainstorming 阶段），待实现

## 背景

D-058 引入了短镜头补齐方案：`<2000ms` 的模型代理 clip 用 `tpad`（克隆末帧）
+ `apad`（补静音）补齐到 2000ms，以绕过 qwen3.8-flash 视频输入 ≥2s 的硬约束
（实测 400 "The video file is too short"）。

补齐方案的问题：模型看到的后半段是冻结画面与静音，对理解没有信息量，反而
可能误导；且短镜头本身内容变化小，视频模态相对图像没有增量价值。

## 决策（用户已确认）

- 对**所有 provider 统一**生效（qwen 与 mimo 通路行为一致），不做按
  provider 的差异化。
- 短镜头取**单张中点帧**作为模型输入（`image_url`），不多帧均抽、不复用
  frame-evidence 的证据帧文件（证据帧是 640px/质量 5 的契约产物，与模型
  输入解耦）。
- 采用**构建层切换**：`build_clips` 直接产出 jpg 代理，transport 层只按
  文件类型选择 part 类型。否决了"图像+视频双输入"（成本翻倍、无必要）与
  "transport 层临时抽帧"（职责混乱、白生成视频代理）。

## 设计

### 1. 构建层 `src/memoloupe/media/clips.py`

- `SHORT_CLIP_MS = 2000` 保留，语义从"补齐阈值"改为**模态切换阈值**。
- `durationMs < SHORT_CLIP_MS` 的镜头：
  - 不生成视频代理；用 ffmpeg `-ss <中点> -frames:v 1 -vf scale=720:-2`
    抽一张 jpg，输出到 `clips/model-proxy/{shotID}-{revision4}.jpg`。
  - 中点取帧复用 `media/frames.py` 的 `representative_time_ms(start, end)`
    （中点 + 夹紧到 `[startMs, endMs-1]`，极短镜头安全）。
- `durationMs >= SHORT_CLIP_MS` 的镜头：视频代理逻辑完全不变
  （宽 720、fps 10、重编码）。
- 删除 `PADDED_MIN_MS`、`proxy_needs_padding`、`proxy_pad_duration_sec` 及
  `model_proxy_argv` 中的 tpad/apad/-shortest 分支。
- `CLIP_BUILD_VERSION` 从 `clips.v3` 升为 `clips.v4`，旧代理缓存失效。
- `modelNormalization`：
  - 图像代理：`strategy = "frame-midpoint-w720"`，`padded` 字段移除
    （该字段仅描述补齐行为，随补齐逻辑一并删除）。
  - 视频代理：`strategy = "reencode-w720-fps10"`（不再有 `+tpad-...` 后缀）。
- 图像代理的 `modelDurationMs` 取镜头真实 `durationMs`（静帧没有可探测
  时长；语义为"模型输入所代表的镜头时长"），不再 ffprobe 代理文件。
  视频代理保持 ffprobe 实测。
- `modelFile` 指向 jpg 相对路径（schema 为自由 string，无格式约束）。

### 2. Transport 层 `src/memoloupe/services/unified_media.py`

- `ModelClip` 按 `proxy_path` 后缀判断模态：`.jpg`（大小写不敏感）→
  构造 `image_url` content part（`data:image/jpeg;base64,...`），其余 →
  现有 `video_url` part 不变。图像 part 不携带 `fps`/`media_resolution`。
- prompt 中的媒体序号说明从"第 N 个 video_url"改为中性表述（"第 N 个
  媒体输入"），并标明每个输入是视频还是图像及其对应 shotID。
- qwen（`QwenChatASR` 所在 provider 的 media 模型）与 mimo 通路统一生效。
  MiMo OpenAI 兼容端点支持 `image_url`；实现后用真实 key 做一次冒烟验证。

### 3. 契约与文档

- `schemas/unified-media.json` **需要变更**（`shortClipPolicy` 的 required
  字段随补齐逻辑失效，且 transport 不再是纯视频）：
  - `schemaVersion` const 2 → **3**；
  - `request.clipTransport` const `videoDataURI` → **`mediaDataURI`**
    （混合 video/image data URI）；
  - `shortClipPolicy` required 改为 `["minimumDurationMs", "imageProxyWidth"]`
    （删除 `recoveryMinimumDurationMs`/`recoveryWidth`）。
- 迁移策略：v2 产物不再通过校验。`output/` 下均为开发样例，重跑即可；
  无对外兼容负担（记录在 D-059）。
- 同步两个生产者（`shot_pipeline.py` 的 skipped stub 与
  `media_orchestrator.py` 的正式 document）、`tests/fixtures/output_full`
  与 `tests/fixtures/minimal` 的 `raw/unified-media.json`。
- `docs/02_DATA_AND_STATE_CONTRACTS.md`：transport 默认值描述同步。
- `docs/03_PIPELINES_AND_ALGORITHMS.md` §2.6：代理策略描述从"短镜头补齐"
  更新为"短镜头中点帧图像代理"。
- `docs/06_DECISIONS_AND_ASSUMPTIONS.md`：新增决策（短镜头模态切换，
  取代 D-058 中的补齐方案；D-058 的 qwen ≥2s 约束记录保留为背景）。
- 注意同步 `shot_pipeline.py` / `media_orchestrator.py` 中引用
  `PADDED_MIN_MS` 的元数据字段（`recoveryMinimumDurationMs`），随补齐
  逻辑一并移除。

### 4. 错误与降级

- 抽帧失败与视频代理构建失败同处理：构建层抛 `ProcessError`，由编排层
  按既有降级矩阵决定；不生成指向不存在文件的路径。

### 5. 已知取舍

- 短镜头的视觉分析基于静帧：运镜类问题模型只能回答静态/未知。这正是
  "短镜头用图像更合理"的题中之义，并在 `modelNormalization.strategy`
  中留有可追溯标记。
- 短镜头失去音频上下文：ASR 是独立窗口化通路，不受影响；视觉组的音频
  感知本来就不是主要证据来源。

## 测试

- `tests/unit/test_clips.py`：
  - 短镜头 → jpg 代理（ffmpeg argv 含 `-frames:v 1`、无 tpad/apad）；
  - 长镜头 → 视频代理 argv 不变；
  - `modelNormalization.strategy` 两种形态；
  - 图像代理 `modelDurationMs == durationMs`。
- transport 测试：`image_url` part 构造（jpeg data URI、无 fps 字段）、
  混合批次（视频+图像）的 prompt 序号说明。
- `test_media_orchestrator.py` / `test_media_evidence.py` 中引用
  补齐语义的断言同步更新。
- 全量测试 + 真实 key 冒烟（qwen 与 mimo 各一次短镜头 E2E）。
