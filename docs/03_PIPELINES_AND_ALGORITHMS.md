# 阶段流水线与算法设计

## 1. 总体执行模型

每个步骤声明：

- 输入 artifact；
- 输出 artifact；
- 配置子集；
- 工具或服务版本；
- 是否必需；
- 指纹算法；
- 失败时是否允许继续。

推荐步骤状态：`pending`、`running`、`complete`、`partial`、`skipped`、`unavailable`、`failed`。

步骤指纹至少包含：

```text
source revision
+ analyzed range
+ relevant upstream fingerprints
+ relevant config only
+ algorithm implementation version
+ external schema/prompt/vocabulary version where applicable
```

不得把 API key、输出路径、生成时间加入语义指纹。

## 2. Phase 1：镜头分析

### 2.1 步骤 DAG

```text
probe_media
  ├─ detect_shots
  │    └─ detect_audio_cuts_and_align
  │          ├─ extract_frames
  │          ├─ build_clips_and_model_proxies
  │          ├─ detect_audio_energy
  │          ├─ detect_quality
  │          ├─ analyze_camera_motion
  │          └─ unified_media_analysis
  ├─ run_asr
  │    └─ detect_audio_music
  └─ capability coverage

全部可用镜头级证据
  └─ resolve_observations
       └─ render_shot_html
            └─ validate_json_and_html
```

实现可并行化独立分支，但输出语义不得依赖线程完成顺序。

### 2.2 媒体探测

使用 ffprobe JSON 输出读取：

- format duration；
- 视频流宽高、帧率、旋转/preferred transform；
- 音频轨道、声道、采样率、语言标签；
- 可选容器与 codec 诊断信息。

要求：

- 正确解析 `30000/1001` 形式帧率。
- 无法取得帧率时写 null，而不是 0。
- duration 优先选择可靠的 format/stream 值并记录来源。
- `revisionID` 使用源文件 SHA-256 前 12 位。CALIBRATION：大文件可先实现完整哈希；后续如需快速指纹，必须区分快速缓存键和内容 revision。
- 分析范围默认整个视频，也可由 CLI 指定。

### 2.3 视觉硬切检测

已知算法常量：

```python
HISTOGRAM_BINS = 254
HARD_CUT_ANALYSIS_SIZE = 128
HARD_CUT_MINIMUM_FRAMES = 8
HISTOGRAM_WEIGHT = 4.61480465
EDGE_WEIGHT = 3.75211168
SCORE_OFFSET = 5.485968377115124
```

推荐实现过程：

1. 用 ffmpeg 按分析 fps 解码，应用视频旋转，缩放到不超过 128 的分析尺寸。
2. 输出固定像素格式的裸帧流，逐帧处理，不把全部帧留在内存。
3. 为每帧计算：归一化颜色直方图、边缘图或边缘统计、平均亮度。
4. 对相邻帧计算 histogram similarity、edge similarity、brightness delta。
5. 计算原始切点 score。已知契约说明“score 越低越像硬切”，因此实现必须保持该方向。
6. 使用 raw negative score 和 adaptive outlier 两条入选路径。
7. 通过最小帧间距抑制重复候选。
8. 将分析范围起止加入边界，生成 shots。

原文未给出相似度的精确公式。初版推荐：

```text
histogram_distance = 1 - histogram_similarity
edge_distance = 1 - edge_similarity
raw_strength = histogram_distance * HISTOGRAM_WEIGHT
             + edge_distance * EDGE_WEIGHT
score = SCORE_OFFSET - raw_strength
```

该公式标记为 CALIBRATION，不属于稳定契约。测试应锁定方向、不变量和已知样例，而不是在没有黄金数据时锁死数值。

候选选择推荐：

- `rawNegativeScore`：score < 0。
- `adaptiveOutlier`：在局部或全局稳健分布中明显偏低，例如低于 median - k*MAD。
- 同一最小镜头窗口中多个候选只保留 score 最低者。
- 首尾不作为普通 hard cut boundary。

极端情况：

- 视频短于最小帧数：输出单镜头。
- 可变帧率：时间来自解码时间戳，不能只用 frameIndex/fps 推导最终时间。
- dissolve/fade/overlay：默认不承诺检测，加入 limitations 和 needsReview。
- 黑场前后可能形成两个候选，不自动合并，交给质量信号和人工复核。

### 2.4 音频切点与视觉边界关联

已知特征：

```python
FEATURE_NAMES = (
    "rmsDb", "zeroCrossingRate", "roughness",
    "amplitudeShape", "autocorrelation1ms", "autocorrelation4ms"
)
MINIMUM_SCALES = (1.5, 0.015, 0.04, 0.025, 0.04, 0.04)
FEATURE_WEIGHTS = (1.0, 0.8, 0.8, 0.6, 0.8, 0.8)
```

协议默认参数示例：

- analysis sample rate：16000 Hz
- frame：20 ms
- novelty threshold：8.0
- sync tolerance：100 ms
- association window：500 ms

处理过程：

1. ffprobe 判断有无音轨。
2. ffmpeg 解码最终混音的首选音轨为 mono PCM s16le。
3. 按固定帧计算六特征。
4. 对相邻或局部窗口特征计算归一化变化，minimum scales 防止极小量纲放大。
5. 按权重形成 novelty score，做峰值选择和最小间隔抑制。
6. 为每个视觉边界寻找关联窗内最佳音频候选。
7. 绝对偏差 <= sync tolerance：`synchronizedCut`。
8. 关联窗内没有切点：`pictureCutAudioContinuous`。
9. 存在音频切点但不在同步窗、不能可靠归因：`audioBoundaryUndetermined`。
10. 无音频：`unavailable`。

关于 final 边界：

- 初版 SHOULD 保守地保留视觉边界，audio-cuts 仅记录关系。
- 若启用 `--align-shot-boundaries-to-audio`，只有 synchronized cut 且置信度达到阈值时才把 final boundary 移到 audioTimeMs。
- 移动后必须重新建立相邻镜头共同边界，保证连续、无重叠和最短镜头约束。
- detected boundary 永不修改。

音频六特征的精确数学定义仍是 CALIBRATION。实现应将特征提取器和峰值选择器拆开，便于替换。

### 2.5 帧证据

默认每镜头至少一张代表帧：

- 取 final 区间中点；
- 对极短镜头向区间内部夹紧；
- 不在精确 endMs 抽帧；
- 输出宽度默认 640，保持纵横比；
- 文件名由 evidenceID 决定。

可选关键帧来自：运动峰值、文字变化、质量异常或人工请求。抽帧失败必须进入 failedFrames，不能生成不存在的 fileRef。

### 2.6 clip 与模型代理

证据 clip：

- 每个 shot 按 final 区间切片；
- 尽量保留音频；
- 路径 `clips/<shotID>.mp4`；
- 允许精确重编码，避免 keyframe copy 导致边界漂移。

模型代理：

- 路径 `clips/model-proxy/`；
- 可统一宽度、fps、编码和音频参数；
- 必须记录 normalization 和 cache key；
- 短于 800 ms 的 clip 可补齐；恢复策略可补到至少 2000 ms、宽度 720；
- 补齐只影响模型输入，不改变证据 clip 和镜头边界。

禁止把外部抽取 JPEG 作为模型视频理解的替代输入。模型 request 的 `externalFrameExtraction=false`。

### 2.7 ASR

ASR 默认对分析范围或全片执行一次，而不是逐镜头请求。供应商结果归一到 `transcript.segments`。

镜头 speech 派生：

- 选择与 shot 区间有正交集的 segments；
- 可按交集比例裁定边界重叠句归属；
- 保留原文顺序；
- 不在此阶段改写或总结原文；
- 跨镜头句可以被多个镜头引用，但故事聚块时仍使用全片 segment 时间。

ASR 失败时：写 failed/skipped 文件，镜头 speech resolver 可退到模型音频结果，但来源必须为 unifiedModel，置信度不应自动等同 ASR。

### 2.8 BGM 检测

目标是确定“有没有 BGM”，不负责风格命名。协议建议 NumPy STFT：

- 在 ASR 语音间隙测量电平、低频能量、谱平坦度；
- 检测 texture rise/fall；
- 形成 music/silent 区间；
- 按区间与镜头重叠比例聚合。

示例阈值：

- sample rate：22050
- music level：-18 dB
- music bass energy：150
- silent level：-22 dB

这些阈值均为 CALIBRATION。初版必须保存 thresholds 和 measurements，并对数据不足返回 unknown。

Observation 规则：

- music → BGM presence=value；bgmStyle 来自模型。
- silent + 可靠覆盖 → BGM presence=absent。
- unknown → unknown；即使模型说“无音乐”，也只能 absent-claimed。

### 2.9 音频能量

按镜头计算短窗 RMS dB，输出 median/min/max 和 label。示例阈值：

```text
silent < -60 dB
low    < -40 dB
medium < -25 dB
high   < -12 dB
peak   >= -12 dB
```

边界是否包含阈值必须在实现中统一并测试。无音轨与测得静音严格区分。

### 2.10 质量检测

建议使用 ffmpeg filters：

- blurdetect
- signalstats
- astats
- blackdetect
- freezedetect

示例默认：video sample fps=2、blur threshold=11、underexposed YAVG=40、overexposed YAVG=215。

实现要求：

- 解析器必须使用机器可识别的 filter 输出，不依赖本地化文案。
- 把事件映射回镜头区间。
- 保存阈值、样本数和聚合测量。
- 多个 flag 可并存。
- 样本不足时 confidence=unknown，即使 flags=[] 也不得声称无问题。

### 2.11 Apple Vision 运镜

推荐方法：

- `VNTrackHomographicImageRegistrationRequest`
- `VNTrackOpticalFlowRequest`

默认采样：2 fps、每镜头最多 12 帧、最大图像维度 960。

每镜头聚合：

- 中位水平/垂直 frame shift；
- scale；
- rotation degrees；
- homography/optical flow 样本数；
- discontinuity indexes；
- motion score。

分类必须谨慎：

- 水平一致位移候选 pan_left/right。
- 垂直一致位移候选 tilt_up/down。
- scale 单调变化候选 zoom_in/out，但 UI 标注“图像尺度变化候选”。
- 不稳定多方向运动候选 handheld。
- 几何突变候选 discontinuity。
- 信号弱为 static；数据不足为 unknown。

阈值为 CALIBRATION。neutralMotions 与原始 evidence 必须保留。

### 2.12 UnifiedMLLM 三组分析

内部推荐三组，最终仍合并为单个 modelShot：

1. `visual`：视觉内容、镜头语言、色彩、光线、文字和合成。
2. `audio`：可听语音、BGM 风格、音效。
3. `editing_function`：素材形态、情绪、语气、转场、连续性。

每组拥有：

- 明确字段子集；
- JSON Schema；
- 注入词表的 prompt；
- group fingerprint；
- 独立 checkpoint。

请求构造：

- 每 batch 默认最多 4 个 clip；
- batch 并发默认 10，但必须配置并受服务限流；
- clip 使用 video Data URI；
- videoFPS 默认 10；
- mediaResolution 默认 default；
- 温度尽量低；
- prompt 强制 shotID 原样返回，不允许额外镜头。

响应解析顺序：

1. 读取结构化响应字段。
2. 读取 message text。
3. 移除单层 Markdown JSON fence。
4. 解析 JSON。
5. schema 校验。
6. 校验请求/响应 shot ID 集合。
7. 逐字段归一化并保留 raw。

禁止用正则从任意长文本中猜测多个 JSON 对象后静默拼接。解析失败应重试或降级。

重试策略：

- 网络暂时错误、429、5xx、非法 JSON 可指数退避重试，默认最多 3 次。
- 批次持续失败后回退到单镜头。
- 主模型不可用可尝试 fallback model。
- 单镜头永久失败写 `permanent_failure`，整体为 partial。
- 每次成功请求后立即 checkpoint。

合并规则：

- 按 shotID 和字段路径合并，不按数组位置。
- 两组不得拥有同一字段；若发生视为 schema 编程错误。
- 缺失字段不得用另一个 shot 的 `s[0]` 之类索引结果补齐。
- 最终 response 每个 modelShot 必须完整；未完成镜头通过 shotStatuses 表达，不伪造成功 response。

### 2.13 Observation 和 HTML

所有检测/模型完成后，为每个 `(shotID, field)` 调 resolver。缺失能力生成 unknown Observation；不得让模板自行推断状态。

渲染后必须先校验再把 CLI 阶段标记为完成。

## 3. Phase 2：故事分析

### 3.1 输入准备

从稳定 JSON 构造每镜头文本摘要：

```text
shotID + time range
+ visual.content
+ subjects/actions/setting
+ ASR speech
+ text overlays
+ transition/continuity
+ deterministic boundary and audio signals
```

不传 clip、代表帧和源文件路径。

### 3.2 确定性候选聚块

默认 `gapMs=1200`，可配置。

算法：

1. 将 ASR segments 按时间排序。
2. 当相邻 segment 间隔 >= gapMs 时创建 speech segment boundary。
3. `segment_of(startMs, endMs)` 为镜头确定主要停顿段。
4. 顺序遍历镜头，当前段号变化时开新 block。
5. `current_seg` 初值使用不会与合法段冲突的 sentinel，保证首镜头创建新 block。
6. 无 ASR 时生成一个或按显式视觉边界规则生成 scaffold；默认单块最保守。

聚块输出是候选，不应因模型分析失败而丢失。

### 3.3 模型叙事字段

模型为每个 block 补充：标题、划分维度和理由、primary role、核心内容、信息作用、叙事密度、观众反应、视觉独立性、块关系和关系理由。

然后把 block 聚合为 slot：slot type、title、block IDs、rationale。

约束：

- 模型不得创建未知 shotID。
- 默认不得改变确定性 block 边界；若启用 model boundary，必须写 `boundarySource=model` 并保留原候选。
- 标题长度约束必须在 schema 和后处理同时检查。
- 关系引用必须闭合。
- 模型失败时 status=scaffold，保留 blocks，语义字段 unknown 或空。

## 4. Phase 3：风格档案

### 4.1 第一趟：确定性聚合

`profile_aggregate.py` 必须是纯函数友好的模块，不调用模型。

至少计算：

- slot 时间范围和 duration share；
- slot 下 block/shot 数；
- 镜头时长 mean、p50；可扩展 p10/p90；
- density curve；
- slot pacing；
- audio boundary alignment；
- transition/framing/camera movement 分布；
- text overlay coverage；
- BGM coverage；
- speech/voice coverage；
- hosted coverage；
- ASR segment、字符和 speech duration 统计；
- turns、nonlinear devices、expectation chains。

统计规则：

- 分布默认按镜头数；如使用时长加权必须在字段或元数据中明确。
- coverage 默认按时间交集占全片分析范围比例。
- slot duration 使用 block 覆盖区间，避免重复计数。
- 所有舍入只在最终序列化时进行，内部保留精度。
- 空数据返回空分布或 null，不除零。

### 4.2 第二趟：模型蒸馏

模型只补充：

- L1 functional title、narrative function、intended reaction；
- L2 carriage、pattern、reference content；
- hook/payoff 表达；
- structure requirements；
- adoption hints；
- discussion items。

模型不得修改确定性时长、shot IDs、block IDs、slot IDs、分布和计数。

蒸馏 prompt 必须强调：

- 抽象参考片的功能，不要求具体地点或对象相同；
- L1 是叙事结构，L2 是承载模式，L3 是原片证据；
- 给出可替代的 affordance，不做一一语义匹配；
- 不生成 Story Spine 或剪辑方案。

## 5. 缓存、恢复与失效

### 5.1 失效矩阵

| 变化 | 必须失效 |
|---|---|
| 源文件 revision | 所有阶段 |
| 分析范围 | 所有区间相关产物 |
| 切镜参数 | shots 及全部镜头级下游 |
| 音频对齐参数 | audio-cuts；若 final 边界变化则全部镜头级下游 |
| 帧参数 | frame-evidence |
| ASR 模型/配置 | asr、music、story、profile speech stats |
| 词表 | 相关模型组归一化、Observation、HTML、story/profile 相关聚合 |
| 统一模型 prompt/schema | 对应 group、合并结果、HTML、story、profile |
| Apple Vision 参数 | camera-motion、相关 Observation、HTML、profile |
| HTML 模板 | HTML 和 HTML 校验，不失效 raw |
| story gap | story-blocks、story HTML、profile |

### 5.2 checkpoint 原则

- checkpoint 文件与最终 artifact 分离。
- 每个 checkpoint 带 fingerprint、状态和完成 ID 集合。
- 只复用通过 schema 校验的成功项。
- 用户可用 `--force STEP` 显式重跑。
- `--no-cache` 忽略复用但仍写新 checkpoint。

## 6. 幂等与并发安全

- 同一 output-dir 同时只能有一个写入型 pipeline，使用明确 lock 文件。
- 校验和只读查看可以并发。
- lock 包含 PID、host、runID 和开始时间；不可仅凭超时删除活锁。
- 原子替换确保读者只看到旧完整版本或新完整版本。
- 数组合并和批次 checkpoint 必须稳定排序。

## 7. 降级矩阵

| 能力不可用 | 行为 |
|---|---|
| ffprobe | Phase 1 致命失败 |
| ffmpeg | 所有媒体处理致命失败 |
| 无音轨 | 音频能力 unavailable，视觉链路继续 |
| ASR | asr failed/skipped，music 降级，模型 speech 可作弱替代 |
| UnifiedMLLM | unified-media partial/failed，模型字段 unknown，HTML 继续 |
| Apple Vision | camera-motion unavailable/缺能力说明，模型运镜保留为主观来源 |
| 故事文本模型 | story scaffold 仍生成 |
| profile 模型 | 确定性 profile 仍生成，distillStatus 非 complete |
| 单帧抽取失败 | 记录 failed frame，其余镜头继续 |
| 单镜头模型失败 | 该镜头 permanent_failure，整体 partial |
