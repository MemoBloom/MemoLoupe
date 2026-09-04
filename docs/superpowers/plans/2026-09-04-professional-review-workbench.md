# MemoLoupe 专业拉片工作台开发计划

日期：2026-09-04  
状态：拟议，待执行  
范围：帧级审片操作、相邻镜头关系分析、可检索时间码转写稿  
建议版本：Phase 06 / Professional Review Workbench

## 1. 背景与目标

MemoLoupe 已经具备镜头切分、音频与视觉分析、故事块、风格档案、证据追溯和人工 correction 闭环，但专业拉片仍存在三个高频断点：

1. 操作者无法仅靠当前 HTML 工作台完成帧级定位、穿梭播放和音画对照；
2. 当前数据主要描述单镜头，缺少“为什么在这里切、前后镜头如何衔接”的 pair 级理解；
3. ASR 已生成时间段，但尚未成为可搜索、可跳转、可筛选和可导出的文本审片入口。

本阶段的目标是把 `shot-analysis.html` 从分析结果展示页升级为可以长时间使用的专业拉片工作台，同时继续遵守 MemoLoupe 的核心原则：确定性优先、失败可见、所有结论可追溯、JSON 为主契约、HTML 为人工校对视图。

阶段完成后，用户应能在同一页面中完成以下主流程：

```text
键盘穿梭与逐帧定位
  → 点击切点检查前后镜头关系及证据
  → 搜索台词并跳到准确时间
  → 对关键字段进行现有 correction / verified 操作
```

## 2. 产品边界

### 2.1 本阶段包含

- `J/K/L` 穿梭控制、逐帧前进/后退、镜头首尾跳转、I/O 临时审片区间、时间码复制；
- 可缩放的统一审片时间线，叠加镜头、波形、ASR、故事块和现有 motion-effects 信息；
- 相邻镜头 pair 的确定性指标、可选模型语义、证据帧/过渡代理和人工复核状态；
- 可检索时间码转写稿、说话人筛选、命中高亮、点击跳转、当前播放位置联动；
- 从现有 `asr.json` 导出 UTF-8 TXT、CSV、SRT、WebVTT 审片副本；
- 对新增 JSON、HTML 语义、跨文件引用、缓存和降级路径的完整校验。

### 2.2 本阶段不包含

- Story Spine 生成、用户素材匹配、自动粗剪或成片输出；
- Premiere、Resolve、Final Cut Pro 工程文件或 FCPXML 导出；
- 多人云协作、评论线程、任务分派；
- ASR 文本与说话人的人工改写；首版只读、检索和导出，避免在没有稳定 segment ID 与迁移规则前扩张 correction 契约；
- 浏览器原生不支持的连续反向解码。`J` 键首版采用基于帧索引的反向步进穿梭，不声称等同 NLE 的负速率回放；
- 仅凭两张静帧把动作匹配、视线匹配或轴线连续性声明为确定事实。

## 3. 强制设计原则

1. 所有时间继续使用整数毫秒，区间继续采用 `[startMs, endMs)`。
2. `detectedStartMs/detectedEndMs` 不因审片操作改变；I/O 只是临时播放状态，只有现有 boundary correction 才能修改 final 边界。
3. 波形、帧时间戳、色彩/亮度差和音量差属于确定性证据；“动作匹配”“视线匹配”“切点动机”等属于语义判断，不得伪装成确定性事实。
4. 模型声称“不存在连续性问题”不得产生 `absent`；模型不可用时，pair 语义必须显式为 `unknown`，确定性指标仍可继续生成。
5. 每个用户可见的 pair 结论必须带一个或多个 `evidenceRefs`；只有对应能力明确未运行时才允许无证据的 `unknown`。
6. 新增 artifact 必须经 `ArtifactStore` 读写、临时文件加原子替换、schema 校验、manifest 指纹登记和跨文件校验。
7. HTML 继续支持纯离线打开；不得依赖 CDN、网络字体或运行时 API。
8. 大视频不得把 PCM 或解码帧整体保存在内存中；波形与帧索引必须流式或分块生成。

## 4. 建议架构

### 4.1 新增 artifact：`raw/review-timeline.json`

这是确定性的审片支持数据，不承载语义分析。建议最小结构：

```json
{
  "schemaVersion": 1,
  "status": "complete",
  "sourceRevisionID": "a1b2c3d4e5f6",
  "analysis": {
    "method": "ffprobe-frame-pts-and-ffmpeg-audio-envelope",
    "algorithmVersion": "review-timeline.v1",
    "analyzedRange": { "startMs": 0, "endMs": 10000 }
  },
  "videoFrames": {
    "status": "complete",
    "timingMode": "pts-index",
    "ptsMs": [0, 42, 83]
  },
  "waveform": {
    "status": "complete",
    "channelMode": "mono-mixdown",
    "binDurationMs": 20,
    "peaks": [[-0.42, 0.38], [-0.31, 0.35]]
  }
}
```

契约要求：

- `ptsMs` 单调不减、位于分析范围内，并保留真实 PTS；不得用平均帧率伪造 VFR 帧时间；
- 无可靠逐帧 PTS 时，`videoFrames.status=unavailable`，UI 降级为按 `media.source.frameRate` 近似步进，并明确显示“近似”；
- 无音轨时 `waveform.status=unavailable`，不是空波形或 `absent`；
- 波形只保存绘制所需的归一化 min/max envelope，不保存 PCM；
- 默认限制波形 bin 数和 HTML 内嵌体积，超长视频按时间范围自适应降采样；
- 指纹至少包含 source revision、analyzed range、ffprobe/ffmpeg 参数、bin 策略和算法版本。

### 4.2 新增 artifact：`raw/shot-relations.json`

该文件以相邻镜头为主索引，pair 数量严格为 `max(shotCount - 1, 0)`。建议 ID 使用稳定的组合形式 `SH0001--SH0002`，避免引入与镜头顺序脱离的新序号。

建议结构分为四层：

- `pair`：leftShotID、rightShotID、boundaryMs；
- `metrics`：确定性色彩、亮度、构图重心、运动方向、响度、语音和音乐连续性指标；
- `semantic`：动作匹配、视线匹配、屏幕方向、空间/时间连续性、切点动机、关系摘要；
- `review`：needsReview、reviewReasons，以及模型与确定性证据冲突。

每个语义字段沿用 Observation 的五态、confidence、source、evidenceRefs、verified 语义。首版不得把 pair 字段写回 `unified-media.json`；`editing.continuity` 由 resolver 从 `shot-relations.json` 提供，保持单一来源。

### 4.3 新增关系分析模块

建议新增：

- `src/memoloupe/analysis/shot_relations.py`：pair 枚举、确定性指标、合并与状态；
- `src/memoloupe/analysis/shot_relation_prompts.py`：受控字段、模型输入白名单和解析；
- `src/memoloupe/services/shot_relation_model.py`：可替换的 pair 语义服务端口；
- `src/memoloupe/media/transition_evidence.py`：切点前后帧和可选短过渡代理生成。

模型输入只允许包含：

- 左镜头末段与右镜头首段的短代理，或明确标记的边界帧；
- 两个镜头已有的结构化摘要；
- ASR、音频边界和确定性差异指标；
- shotID、时间范围和证据引用。

模型不得修改镜头边界、增加/删除镜头或覆盖确定性指标。

### 4.4 HTML 视图模型

`shot-analysis.html` 继续由 Python 渲染全部必要数据，浏览器侧只处理播放、筛选、搜索和临时 UI 状态。由于 `file://` 下 `fetch()` 行为不可靠，首版将经过 HTML escape 的紧凑审片数据放入固定的 `<script type="application/json">` 节点；不得嵌入视频、PCM、Data URI 或模型原始响应。

新增三个协同区域：

1. 播放器工具条：时间码、帧号/近似状态、J/K/L、逐帧、I/O、循环、复制；
2. 多轨时间线：故事块、镜头、切点关系、ASR、波形、motion-effects；
3. 右侧检查器：镜头详情、切点关系、转写稿三个标签页。

## 5. 实施计划

### Phase 06-00：契约与性能基线

目标：在写交互和算法前冻结两个新增 artifact 及浏览器性能门槛。

任务：

- [ ] 在 `docs/07_SOURCE_DATA_CONTRACT.md` 定义 `review-timeline.json` 与 `shot-relations.json`；
- [ ] 在 `docs/02_DATA_AND_STATE_CONTRACTS.md` 补充 pair ID、顺序、状态、引用和缓存不变量；
- [ ] 在 `docs/03_PIPELINES_AND_ALGORITHMS.md` 定义两个新阶段的位置与降级矩阵；
- [ ] 在 `docs/04_UI_AND_VALIDATION.md` 定义播放器、时间线、pair DOM 与 transcript DOM 的机器语义；
- [ ] 在 `docs/05_TESTING_AND_ACCEPTANCE.md` 增加帧定位、长片性能和搜索验收；
- [ ] 在 `docs/06_DECISIONS_AND_ASSUMPTIONS.md` 记录浏览器帧精度、反向穿梭、波形 bin 和 pair 模型边界；
- [ ] 新增 `schemas/review-timeline.json`、`schemas/shot-relations.json` 和合法/非法 fixtures；
- [ ] 将两个名称注册到 `ArtifactName`、manifest、校验器和 completion 的可选能力列表；
- [ ] 用 1 分钟 CFR、1 分钟 VFR、无音轨、60 分钟长片建立基线样例或可生成 fixture。

验收：

- 两个 schema 能拒绝非法状态、越界时间、无效 shotID 形状和缺失 evidenceRefs；
- 单镜头输入允许合法的空 relations；多镜头 relations 必须严格覆盖每个相邻 pair；
- 文档明确“精确帧索引”和“平均帧率近似”是两种不同状态；
- 未开始任何 UI 功能时，全量现有测试仍通过。

预计：3–4 工程日。

### Phase 06-01：确定性审片索引与波形

目标：产生播放器可以可靠消费的帧 PTS 和轻量波形数据。

任务：

- [ ] 新增 `src/memoloupe/media/review_timeline.py`；
- [ ] 使用 ffprobe 的逐帧/packet 时间戳构建 `ptsMs`，处理非零起始 PTS、重复 PTS、旋转和 VFR；
- [ ] 使用 ffmpeg 分块解码音频，生成固定上限的 min/max envelope；
- [ ] 为无音轨、ffprobe 不支持逐帧输出、超时和部分分析范围生成显式降级状态；
- [ ] 接入 `ShotAnalysisPipeline`，默认在基础 media/shots 完成后运行；
- [ ] 增加 `--skip build_review_timeline` 与 `--force build_review_timeline`；
- [ ] 指纹命中时复用，损坏、状态非 complete 或版本变化时重建；
- [ ] 在 `validate_output_dir` 中校验帧 PTS、波形范围和 source revision。

重点测试：

- CFR 与 VFR 的上一帧/下一帧索引；
- 第一帧、最后一帧和 `[startMs, endMs)` 边界；
- 无音轨输出 unavailable；
- 超长音频不超过设定 bin 上限；
- 同指纹二次运行不再调用 ffprobe/ffmpeg；
- ffmpeg 失败不阻断现有 shot/story HTML 骨架生成。

验收：

- VFR 样例的 `ptsMs` 来自实际时间戳，不由平均帧率推导；
- 60 分钟样例的 artifact 与 HTML 增量体积在决策文件设定的预算内；
- `memoloupe validate OUTPUT --strict` 能发现 PTS 逆序和波形范围错误。

预计：4–6 工程日。

### Phase 06-02：专业播放器与统一时间线

目标：让用户不离开 MemoLoupe 即可完成大部分定位和反复观看操作。

任务：

- [ ] 在 `shot_html.py` 生成紧凑 review timeline 视图模型；
- [ ] 在 `templates/shot-analysis.html` 增加当前时间码、帧号和精度状态；
- [ ] 实现快捷键：`K/Space` 暂停切换、`L` 多档正向速度、`J` 多档反向步进、左右键逐帧、Shift+左右跳镜头、`I/O` 设临时区间、Esc 清除区间；
- [ ] 输入框、select、textarea 聚焦时禁用全局快捷键，避免编辑内容时误触；
- [ ] 使用 `requestVideoFrameCallback` 同步当前帧；不支持时降级为 `timeupdate/seeked`；
- [ ] 时间线支持滚轮/按钮缩放、拖拽平移、点击定位、播放头跟随和当前镜头高亮；
- [ ] 波形、镜头、故事块、ASR、motion-effects 与切点共享同一时间坐标；
- [ ] 提供可复制的 `HH:MM:SS.mmm` 时间码和键盘帮助浮层；
- [ ] 保留现有循环镜头、needsReview 过滤、correction 保存与确认行为。

交互验收：

- 连续按左右键不会越过分析范围；到达镜头 `[startMs, endMs)` 的 end 时定位到下一镜头首帧；
- VFR 在存在 PTS 索引时按索引步进；索引 unavailable 时页面明显显示“按平均帧率近似”；
- `J` 不伪装成原生倒放，界面帮助明确说明其为反向帧穿梭；
- I/O 不写 raw、不改变 final 边界，刷新后可以丢弃；
- 页面只用键盘也能定位、播放、打开检查器和关闭帮助；
- 所有新增控件具有可访问名称和清晰 focus 状态。

自动化测试：

- Python 渲染与 HTML contract 单元测试；
- 浏览器级快捷键、逐帧、缩放、输入焦点隔离和无音轨测试；
- 10、100、500、2000 镜头的渲染与交互性能基线；
- Safari/WebKit 与 Chromium 至少各跑一套关键路径。

预计：6–8 工程日。

### Phase 06-03：相邻镜头关系确定性层

目标：先回答“切点前后发生了哪些可测量变化”，不依赖模型也能形成有用的关系视图。

任务：

- [ ] 新增 `transition_evidence.py`，为每个切点生成 left-exit/right-entry 证据帧；
- [ ] 枚举严格相邻 pair，并从 `shots.json` 派生 boundaryMs；
- [ ] 从现有 artifacts 聚合以下确定性指标：
  - 边界前后亮度、主色/直方图和构图重心差；
  - camera-motion 的方向与强度变化；
  - audio-energy 的响度差；
  - audio-cuts 的同步/连续信号；
  - music-flags 的音乐状态连续性；
  - ASR 的跨切点语音覆盖与停顿长度；
- [ ] 不足以可靠计算的指标写 unknown/unavailable，不从缺失数组推断没有变化；
- [ ] 以阈值规则产生“需复核”候选，而不是直接产生剪辑评价；
- [ ] 写入 `raw/shot-relations.json` 并接入指纹、checkpoint、strict validator。

验收：

- N 个镜头严格生成 N-1 个 pair，顺序与 `shots.json` 一致；
- 每个 metric 的证据引用能解析到正确的两个 shot 或边界证据；
- 无音轨、无 ASR、无 camera-motion 时仍能生成 partial artifact；
- 确定性层不得输出“动作匹配”“越轴”或“切点有创意”等模型语义结论。

预计：5–7 工程日。

### Phase 06-04：相邻镜头关系语义层与人工复核

目标：在确定性指标上增加可选语义判断，并把不确定性与冲突清楚呈现给人。

任务：

- [ ] 在 `rules/vocabulary.json` 定义 pair 语义受控词表；
- [ ] 新增 pair prompt 与解析器，字段建议包括：
  - `actionContinuity`：动作承接/动作跳跃/无法判断；
  - `eyelineContinuity`：视线承接/视线冲突/不适用/无法判断；
  - `screenDirection`：方向连续/方向反转/不适用/无法判断；
  - `spatialTemporalRelation`：同空间连续/跨空间/时间跳跃/蒙太奇并置/无法判断；
  - `editMotivations`：动作、对白、声音、节拍、视觉匹配、信息揭示、对比、时空推进；
  - `relationSummary`：简短、可人工复核的解释；
- [ ] 对每个 pair 使用白名单证据生成模型输入；
- [ ] 模型异常、非法 JSON、漏 pair、未知 pair、枚举越界均不得污染确定性结果；
- [ ] 合并时保留 raw metrics、模型原文摘要、解析后 Observation 和冲突原因；
- [ ] 在时间线切点上显示 review badge；点击打开 A/B 边界帧、指标、语义和 evidence drawer；
- [ ] 为 `editing.continuity` 新增 resolver，但只从 `shot-relations.json` 读取；
- [ ] pair 的 verified/correction 如进入首版，必须复用现有 correction 追加历史，并以组合 pair ID 为 entityID；不得修改 raw relation。

模型评估集至少覆盖：

- 动作匹配切、视线匹配切、方向反转、越轴嫌疑；
- 声音先行/延续、对白跨切、节拍切、图形匹配；
- 纯蒙太奇与故意跳切，避免把风格选择一律标成错误；
- 静帧不足以判断动作时必须输出 unknown；
- 中英文对白、无对白、极短镜头、黑场和叠化边界。

验收：

- 模型不可用时页面仍显示确定性 pair 指标；
- 每条语义结论都可跳到 left/right 证据并看到来源；
- 冲突只产生 needsReview，不自动选择“正确”结论；
- 模型不得改变 pair 集合、镜头边界或指标数值；
- 人工核实状态与五态保持独立。

预计：7–10 工程日，另需真实样片校准。

### Phase 06-05：可检索时间码转写稿

目标：把已有 ASR 从镜头字段升级为文本驱动的审片入口。

任务：

- [ ] 在 Python 渲染层把 ASR segment 映射到 shotID 与 storyBlockID，只生成视图模型，不复制或改写 ASR 主事实；
- [ ] 每个 cue 携带 startMs、endMs、speaker、confidence 和 `raw/asr.json#transcript.segments[N]`；
- [ ] 增加全文搜索、Unicode/CJK 大小写归一化、命中高亮和结果计数；
- [ ] 支持按说话人、镜头、故事块、时间范围和低置信度筛选；
- [ ] 点击 cue 或搜索结果跳到 startMs，并允许播放到 endMs；
- [ ] 播放时自动高亮当前 cue，手动滚动后暂时停止自动跟随；
- [ ] 在统一时间线上渲染语音区间，搜索过滤不改变原始 segment 顺序；
- [ ] 实现 client-side TXT/CSV/SRT/WebVTT 导出，明确标为审片副本而非新核心 artifact；
- [ ] SRT/VTT 导出处理重叠、零长度、越界、换行和 HTML escape；
- [ ] ASR skipped/failed、segments 为空和 speaker 缺失时显示明确状态。

搜索首版范围：

- 支持精确子串和规范化子串；
- 不做向量检索、同义词扩展或模型问答；
- 不把“没有搜索结果”解释为视频中确定不存在该内容。

验收：

- 搜索结果跳转误差不超过对应 ASR segment 的起点精度；
- 同一 segment 可从转写稿、时间线和所属镜头互相定位；
- 导出的 SRT/VTT 能被标准播放器读取，所有时间位于分析范围内；
- ASR 不可用时页面不出现伪造空转写稿；
- 5000 个 cue 的搜索与滚动仍满足性能预算。

预计：4–6 工程日。

### Phase 06-06：整体验收、校准与发布

目标：证明三个能力在真实长视频、降级环境和 correction 流程下能共同工作。

任务：

- [ ] 增加端到端 fixture：CFR、VFR、无音轨、连续对白、密集切镜、长视频；
- [ ] 建立至少 5 条人工标注样片的 pair 关系黄金集；
- [ ] 校准 review reason 阈值，记录 precision/recall 和典型误报；
- [ ] 验证重新生成 HTML 不改变 raw artifacts 和既有 corrections；
- [ ] 验证 final boundary correction 后 review timeline、pair 与 transcript 映射正确失效或重建；
- [ ] 运行 schema、cross-artifact、HTML、集成和全量测试；
- [ ] 生成一个真实样片产物并运行 strict validate；
- [ ] 更新用户文档、快捷键表、降级说明和隐私说明。

发布门槛：

- [ ] `uv run pytest -q` 全量通过；
- [ ] `uv run memoloupe validate <output-dir> --strict` 为 0 error；
- [ ] 页面离线打开和 review server 两种模式均可用；
- [ ] source revision 变化后旧 pair correction 不会自动套用；
- [ ] 模型、ASR、Apple Vision 任一不可用时仍可生成可解释页面；
- [ ] 60 分钟/1000 镜头样例满足已记录的生成时间、HTML 体积、首次交互和搜索延迟预算；
- [ ] 未出现 API key、完整授权头、媒体 Data URI 或绝对敏感路径泄露。

预计：4–6 工程日。

## 6. 依赖与关键路径

```text
06-00 契约冻结
  ├─→ 06-01 审片索引 ─→ 06-02 播放器与时间线 ─┐
  ├─→ 06-03 pair 确定性层 ─→ 06-04 pair 语义层 ├─→ 06-06 整体验收
  └─→ 06-05 时间码转写稿 ──────────────────────┘
```

关键路径是 `06-00 → 06-03 → 06-04 → 06-06`。`06-01` 完成后，播放器 UI 与转写稿 UI 可以并行开发；pair 确定性层也可以与播放器 UI 并行，但三者在统一时间线合并前必须共享同一时间坐标工具。

## 7. 建议人员拆分与工期

没有历史 velocity 和人员可用性数据，因此这里只给工程日区间，不承诺 story points。

| 工作流 | 建议负责人能力 | 工程日 |
|---|---|---:|
| 契约、ArtifactStore、validator | Python / schema | 3–4 |
| ffprobe/ffmpeg 帧索引与波形 | 媒体工程 | 4–6 |
| 播放器与时间线 | 原生 HTML/JS / 浏览器媒体 | 6–8 |
| pair 确定性分析 | 媒体算法 | 5–7 |
| pair 模型语义与评估 | LLM/视觉模型 | 7–10 |
| transcript 搜索与导出 | 前端 + ASR 数据 | 4–6 |
| E2E、性能、校准、文档 | QA / 综合 | 4–6 |
| **基础合计** |  | **33–47** |
| **20% 风险缓冲后** |  | **40–57** |

建议排期：

- 单人串行：约 8–11 周；
- 两人并行：约 5–7 周；
- 三人并行：约 4–6 周，但 06-00 契约和 06-06 集成仍应集中负责。

## 8. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 浏览器无法提供 NLE 等级反向播放 | J 键体验与预期不符 | 明确实现反向帧穿梭；先做可验证原型，UI 标注能力边界 |
| VFR seek 与实际展示帧不一致 | “逐帧”不可信 | 使用真实 PTS 索引和 `requestVideoFrameCallback`；无索引时明确降级为近似 |
| 全量帧索引和波形增大 HTML | 长片打开慢 | 紧凑整数数组、波形 bin 上限、性能 fixture、必要时只嵌入当前分析范围 |
| 两帧不足以判断动作/轴线 | pair 模型误报 | 使用短边界代理；证据不足必须 unknown；所有高风险判断进入 needsReview |
| 故意跳切被当成连续性错误 | 误导创作判断 | 输出“现象/关系”而非好坏评分，词表包含蒙太奇和故意跳切语义 |
| ASR segment 没有稳定 ID | 无法安全保存文本 correction | 首版只读；后续先升级 ASR 契约和迁移策略再开放编辑 |
| final 边界修正导致派生结果陈旧 | pair/转写映射错位 | 指纹纳入 final shots；boundary correction 后显式标记相关 artifact outdated |
| 模型成本随 N-1 pair 增长 | 长片调用昂贵 | 先运行确定性层，只对 needsReview 或用户选择的 pair 调模型，并逐 pair checkpoint |

## 9. 推荐的最小可交付版本

为了尽快验证价值，第一轮可在完成 06-00 后交付一个不依赖模型的 MVP：

1. CFR/VFR 帧索引、波形和逐帧快捷键；
2. 统一时间线与 ASR 搜索跳转；
3. N-1 pair 的亮度、运动、响度、语音跨切和音频边界指标；
4. 点击切点查看 A/B 边界帧；
5. 暂不开放 pair 模型语义和 transcript correction。

该 MVP 预计 18–25 工程日，已经能够验证用户是否愿意把 MemoLoupe 当作主拉片页面持续使用。验证通过后再进入 06-04 的高不确定性模型语义层。

## 10. 完成定义

本阶段只有在以下用户任务全部能闭环时才算完成：

1. 用户打开一条 VFR 视频，能看到精度状态并逐展示帧前后移动；
2. 用户按 J/K/L 穿梭、设 I/O、缩放时间线，且不会误改镜头边界；
3. 用户点击任意切点，能看到两个镜头、确定性差异、语义判断、置信度与证据；
4. 用户搜索一句台词，能从结果跳到对应时间，并在播放过程中看到 cue 联动；
5. 用户可以导出合法的 SRT/VTT/TXT/CSV 审片副本；
6. 模型或 ASR 不可用时，页面以显式状态降级而不是伪造空结果；
7. 所有新增 artifact 和 HTML 都能通过 strict schema、跨文件和语义校验；
8. 现有 shot → story → profile、correction、确认和重渲染流程无回归。

