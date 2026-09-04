# MemoLoupe 代码复现总规格

状态：实现基线 1.0  
面向：开发型 AI、架构师、测试工程师  
目标：从空仓库构建可运行的 MemoLoupe 参考实现

## 1. 产品定义

MemoLoupe 是面向“拉片”的分阶段媒体分析系统。它把一支参考视频转换为可播放、可人工校对、可机器追溯的结构化理解。

系统依次产生三类最终产物：

1. Phase 1：`shot-analysis.html`，展示镜头时间线和镜头级观察。
2. Phase 2：故事分析结果并入 `shot-analysis.html`，展示故事块、故事插槽及叙事作用。
3. Phase 3：`style-profile.json`，表达可复刻的叙事、节奏和风格协商契约。

最终边界止于 `style-profile.json`。系统 MUST NOT：

- 为用户生成 Story Spine；
- 将参考片镜头与用户素材做一一匹配；
- 自动生成粗剪；
- 输出成片；
- 把参考片的具体语义内容当作下游硬性复刻要求。

后续素材管理、混剪规划或 FCPXML 导出是外部消费者，不属于本实现。

## 2. 核心方法

系统按证据可靠性分层：

```text
ffprobe/ffmpeg/数学测量
        ↓
确定性 raw 证据
        ↓
ASR / Apple Vision / UnifiedMLLM
        ↓
Observation 语义归一化
        ↓
JSON 主契约 + HTML 人工校对
        ↓
确认后的故事分析与风格蒸馏
```

核心原则是“越往下越确定，越往上越语义化”。能通过媒体信号直接测量的内容 MUST NOT 委托模型作为唯一来源。

## 3. 角色与使用场景

### 3.1 拉片操作者

- 选择视频和输出目录。
- 运行 Phase 1。
- 在 HTML 中播放镜头、查看证据、修正字段并标记核实。
- 确认镜头分析后运行 Phase 2。
- 校对故事块和插槽后运行 Phase 3。

### 3.2 自动化消费者

- 读取 JSON，不解析 HTML 作为主数据。
- 使用 style profile 的 L1/L2 进行结构匹配。
- 使用镜头级数据作为节奏、能量、视觉风格软特征。
- 使用质量标记进行硬过滤。

### 3.3 开发与调试人员

- 能从任一 raw 证据定位到最终单元格。
- 能查看每个阶段的状态、配置指纹和失败原因。
- 能只重跑失效的检测器、模型组或镜头批次。

## 4. 系统不变量

以下约束在所有实现和版本中 MUST 成立。

### 4.1 确定性优先

- 媒体元数据、边界、时长、响度、黑场、冻结、音频削波等优先由检测器产生。
- 模型可以补充解释，但不得覆盖原始测量。
- 冲突时必须同时保留原始证据、模型意见和解析后的 Observation。

### 4.2 五态严格语义

每个受控分析值只能处于：

- `value`
- `absent`
- `absent-claimed`
- `unknown`
- `unmapped`

`absent` 只能由明确授权的确定性检测器产生。模型输入中出现“无”“没有”“不存在”等结论时，解析器必须降级为 `absent-claimed`。

### 4.3 核实状态独立

`verified` 表示人是否核实该 Observation，不表示该值存在或正确。以下组合都合法：

- `state=value, verified=false`
- `state=unknown, verified=true`
- `state=absent, verified=true`
- `state=unmapped, verified=false`

### 4.4 全链路追溯

- 每个用户可见的分析单元格必须携带至少一个 `evidenceRef`，除非状态为 `unknown` 且对应能力明确未运行。
- 引用必须指向已有文件、数组项、clip 或帧。
- 人工修正必须记录原值、修正值、时间和来源，不得破坏原 raw 证据。

### 4.5 边界双轨

镜头必须同时保留：

- `detectedStartMs` / `detectedEndMs`
- `finalStartMs` / `finalEndMs`

音频对齐或人工修正只更新 final 边界。检测边界不可变，除非重新运行视觉检测并生成新的源修订或分析指纹。

### 4.6 阶段可恢复

- 每项昂贵操作必须有输入指纹。
- 完成一个独立请求或检测器后立即 checkpoint。
- 重跑时只复用状态完整、指纹匹配、引用文件存在的产物。
- 不得仅因文件存在就视为缓存有效。

### 4.7 失败可见

- 不可用、跳过、部分成功和失败必须显式编码。
- 不得用空数组伪装“检测结果为空”，除非该文件状态为 complete 且空数组确实表示确定性无结果。
- 模型失败不应阻止确定性 HTML 骨架生成。

## 5. 三阶段业务规则

### 5.1 Phase 1：镜头分析

输入：原视频、可选分析范围、配置和服务凭据。  
主要过程：媒体探测、视觉切镜、音频检测和对齐、clip/帧证据、ASR、质量与能量分析、Apple Vision、统一模型、Observation 归一化、HTML 渲染和校验。  
输出：`raw/*.json`、`clips/`、`evidence/frames/`、`shot-analysis.html`。

Phase 1 完成不等于人工确认。HTML 文档状态至少要区分 `draft`、`underReview`、`confirmed`、`outdated`。

### 5.2 Phase 2：故事分析

输入：已生成的镜头契约，默认要求 shot analysis 为 confirmed；开发模式可通过显式参数允许 draft。  
主要过程：按 ASR 停顿生成确定性候选块，将镜头文本摘要送入文本模型，产生故事块字段和故事插槽。  
输出：`raw/story-blocks.json`；故事块与故事插槽渲染进 `shot-analysis.html`，不再生成独立的 `story-analysis.html`。

Phase 2 的重跑入口统一为 `memoloupe shot --story-only`；不存在独立的 story 子命令。

Phase 2 MUST NOT 把视频 clip 发送给叙事模型。叙事模型只能看到结构化镜头摘要、ASR 和必要的确定性信号。

### 5.3 Phase 3：风格档案

输入：镜头、故事块、故事插槽和各类 raw 统计。  
过程分两趟：先确定性聚合，再可选模型蒸馏。  
输出：`style-profile.json`。

模型不可用时仍应输出完整结构，主观字段置 `null`、空数组或 `unknown`，并将 `distillStatus` 标为 `unavailable`、`skipped` 或 `empty`。

## 6. 输出目录

推荐物理布局：

```text
output-dir/
├── manifest.json
├── raw/
│   ├── media.json
│   ├── shots.json
│   ├── audio-cuts.json
│   ├── frame-evidence.json
│   ├── asr.json
│   ├── music-flags.json
│   ├── unified-media.json
│   ├── camera-motion.json
│   ├── quality-flags.json
│   ├── audio-energy.json
│   └── story-blocks.json
├── clips/
│   ├── SH0001.mp4
│   └── model-proxy/
├── evidence/
│   └── frames/
├── checkpoints/
├── corrections/
├── shot-analysis.html
└── style-profile.json
```

旧名称 `raw/shot-candidates.json` 可以通过 manifest 或兼容链接映射到逻辑名称 `shots.json`。新业务代码 SHOULD 只通过 ArtifactStore 获取路径，禁止散落硬编码文件名。

## 7. 非功能要求

### 7.1 可重复性

- 同一源修订、相同配置和无模型随机性的确定性阶段应产生语义等价输出。
- 数组顺序必须稳定。
- JSON 使用 UTF-8、稳定缩进和稳定 key 排序策略。
- 生成时间不参与内容指纹。

### 7.2 可移植性

- 核心 Python 代码应独立于当前用户名和绝对目录。
- Apple Vision 能力只在 macOS 启用；其他平台返回 unavailable，不阻止其余阶段。
- HTML 应尽量离线运行，不依赖外部 CDN 或脚本。

### 7.3 安全与隐私

- 日志不得打印 API key、完整授权头或视频 Data URI。
- 默认不上传完整原视频，只上传单镜头模型代理。
- 请求日志只记录模型、镜头 ID、字节数、耗时、状态和脱敏错误。
- 输出中的绝对源路径属于敏感信息；导出或分享模式 SHOULD 支持路径脱敏。

### 7.4 性能

- ffmpeg 进程必须受全局信号量限制。
- 模型请求并发与媒体处理并发分别配置。
- 大视频不得一次性把全部解码帧保存在内存。
- 音频 PCM 可以流式或分块处理；若采用整片加载，必须设上限并提供降级策略。

## 8. 完成定义

兼容实现至少满足：

1. 对有音轨和无音轨视频都能完成 Phase 1。
2. 镜头 final 区间连续、升序、无重叠且覆盖分析范围。
3. 所有 raw JSON 通过 schema 和跨文件校验。
4. 模型不可用时生成可播放、可校对的降级 HTML。
5. 人工修正可保存并在重渲染后保留。
6. Phase 2 不读取视频也能生成故事脚手架。
7. Phase 3 在无模型模式下仍生成确定性统计。
8. 中断后重跑不重复已成功且指纹有效的昂贵任务。
9. 校验器能发现错误状态、断裂引用、重复 ID、模板混用和非法 HTML 属性。
10. 测试与验收要求全部满足 `05_TESTING_AND_ACCEPTANCE.md`。
