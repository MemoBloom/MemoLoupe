# 数据与状态契约实现设计

本文件描述如何实现和校验数据契约；完整字段表与示例见 [07_SOURCE_DATA_CONTRACT.md](07_SOURCE_DATA_CONTRACT.md)。两者冲突时，字段定义以来源契约为准，状态不变量和实现安全约束以本文件为准，并在 `06_DECISIONS_AND_ASSUMPTIONS.md` 登记冲突。

## 1. 通用约定

### 1.1 编码与版本

- JSON MUST 使用 UTF-8。
- 中文受控值 MUST 原样保存，不进行 Unicode ASCII 转义作为语义要求；序列化器推荐 `ensure_ascii=False`。
- 稳定数据契约版本为 `1.0`。
- `style-profile.json.schemaVersion` 当前为整数 `2`，与整体契约版本分开演进。
- 每个 schema 应设置 `$id` 和明确的附加字段策略。

### 1.2 时间

- 业务时间使用整数毫秒，字段以 `Ms` 结尾。
- 统计展示可使用秒，字段以 `Sec`、`Seconds` 结尾。
- 区间统一为 `[startMs, endMs)`。
- `endMs` 必须大于 `startMs`，点证据除外。
- 帧证据允许 `range.startMs == range.endMs`，表示时间点，不表示空区间。
- 浮点秒转换到毫秒时必须使用项目统一舍入函数，禁止各模块自行 `int()`。

推荐：使用 decimal 或显式四舍五入到最近毫秒；同一 ffprobe 时间戳在各文件中必须得到相同整数。

### 1.3 ID

| 实体 | 格式 | 示例 |
|---|---|---|
| 镜头 | `SH` + 4 位数字 | `SH0001` |
| 音频切点 | `AU` + 4 位数字 | `AU0001` |
| 帧证据 | `F_<shotID>_<MAIN\|Knn>` | `F_SH0001_MAIN` |
| 故事块 | `B` + 4 位数字 | `B0001` |
| 故事插槽 | `S` + 3 位数字 | `S001` |

ID 在各自实体集合中必须唯一。Unified media 的批次 ID 与故事块 ID 分属不同文件命名空间；代码不得仅凭字符串推断实体类型。

### 1.4 缺失、null、unknown 和空集合

- 字段不存在：旧版本、能力未运行或文件不合规。
- `null`：字段适用，但本次没有可用值，或模型未返回。
- `unknown`：字段已被分析，但无法确认。
- 空数组：分析已完成且集合确实为空；如果分析未完成，必须依赖文件状态表达。
- 空字符串不是缺失值，除非某字段 schema 明确允许；核心语义字段 SHOULD 拒绝空字符串。

### 1.5 置信度

- 语义置信度：`high`、`medium`、`low`、`unknown`。
- 原始指标置信度可以是 `0.0..1.0`。
- 不得把数值检测器分数直接当成概率，除非算法明确校准。

## 2. 证据引用

JSON 中 `evidenceRefs` 为字符串数组。允许：

```text
raw/media.json#source.durationMs
raw/shots.json#shots[0]
raw/unified-media.json#batches[0].response.shots[0].visual.framing
clips/SH0001.mp4
evidence/frames/F_SH0001_MAIN.jpg
```

解析器必须：

1. 拒绝绝对路径和 `..` 逃逸。
2. 解析 `#` 前的相对文件路径。
3. 对 JSON 引用解析当前约定的点号与数组下标语法。
4. 验证文件存在、索引合法、引用实体与当前 shot 一致。
5. 在 HTML 中转为空格分隔的转义字符串，但 JSON 始终保留数组。

推荐未来迁移到 RFC 6901 JSON Pointer，但契约 1.0 必须兼容当前示例语法。

## 3. Observation

Observation 是渲染层和人工校对层使用的统一语义单元，不要求单独成为 raw 文件。

### 3.1 状态机

| 输入情况 | state | value |
|---|---|---|
| 合法具体值 | `value` | 归一化值 |
| 确定性检测器确认不存在 | `absent` | `null` 或标准缺席表示 |
| 模型声称没有 | `absent-claimed` | `null` 或原始“无”保存在 originalValue |
| 已运行但无法确认 | `unknown` | `null` |
| 模型有内容但词表无法映射 | `unmapped` | 原始内容或 `null`，originalValue 必填 |

禁止的状态转换：

- 模型 `absent-claimed` 自动提升为 `absent`。
- 用户只点击 verified 就把 `unknown` 改为 `absent`。
- 词表更新后静默把历史 `unmapped` 改值而不重算或记录迁移。

### 3.2 来源

推荐 source 枚举：

- `ffprobe`
- `ffmpeg`
- `audioDetector`
- `appleVision`
- `asr`
- `unifiedModel`
- `textModel`
- `aggregate`
- `human`
- `fallback`

当 Observation 来源为人工修正时，应保留 `originalValue`、原 source 和 correction 引用。

### 3.3 受控词表

`rules/vocabulary.json` 至少支持：

```json
{
  "version": 1,
  "fields": {
    "visual.framing": {
      "values": ["远景", "全景", "中景", "近景", "特写"],
      "aliases": {"wide shot": "全景"},
      "allowTransitions": true,
      "multiValueSeparator": "、"
    }
  }
}
```

接口建议：

```python
def normalize(field: str, raw: object) -> NormalizationResult: ...
def canonical_key(field: str, value: str) -> str | None: ...
def prompt_fragment(field: str) -> str: ...
```

归一化必须保留原始字符串。允许 `A → B` 的字段应逐项归一化，不允许转换箭头内外的自由文本绕过词表。

## 4. 文件契约摘要

### 4.1 `raw/media.json`

顶层：`source`。

必需内容：

- `assetID`
- `sourcePath`
- `revisionID`：源文件内容 SHA-256 前 12 位
- `durationMs` / `durationSec`
- `frameRate`
- `resolution.width` / `resolution.height`
- `aspectRatio`
- `audioTracks[]`
- `analyzedRange.startMs` / `endMs`
- `analysisCoverage[]`

关键约束：

- analyzed range 位于 `[0, durationMs]`。
- `durationSec` 与 `durationMs / 1000` 在容差内一致。
- `aspectRatio` 在宽高有效时与 `width / height` 一致。
- audio track ID 在本文件内唯一。
- analysis coverage 必须能区分 complete、partial、unavailable、skipped、failed。

### 4.2 `raw/shots.json`

顶层：`analysis`、`boundaries`、`shots`。

`analysis.method` 固定为 `memoClipHardCutCandidateCuts`。`shots` 是全部镜头级文件的主索引。

每个 shot 必须包含：

- `shotID`
- `sequenceIndex`
- detected/final 起止时间
- `durationMs`
- `boundaryIn` / `boundaryOut`
- `needsReview`

关键约束：

- `sequenceIndex` 从 1 连续递增。
- `durationMs == finalEndMs - finalStartMs`。
- 镜头按 final start 升序。
- 镜头 final 区间无重叠。
- 正常完整分析时，相邻镜头应满足前一 end 等于后一 start。
- 首镜头 boundaryIn 为 sourceStart，末镜头 boundaryOut 为 sourceEnd。
- boundary score 越低越像硬切；不得在 UI 中误标为“越高越可信”。
- `selectedBoundaryCount` 与有效边界数量一致。

### 4.3 `raw/audio-cuts.json`

顶层：`status`、`analysis`、`boundaries`、`shots`。

classification 允许：

- `sourceStart`
- `sourceEnd`
- `synchronizedCut`
- `pictureCutAudioContinuous`
- `audioBoundaryUndetermined`
- `unavailable`

关键约束：

- `offsetMs == audioTimeMs - visualTimeMs`。
- synchronized cut 的绝对 offset 不超过 `syncToleranceMs`。
- 每个 shotID 必须存在于 shots.json。
- `audioBoundaryID` 引用必须存在于 boundaries。
- 无音轨时 status 为 unavailable，并为镜头边界输出 unavailable 分类，而不是省略文件。
- 本文件不输出 J-cut/L-cut，因为最终混音无法可靠归因。

### 4.4 `raw/frame-evidence.json`

顶层：`status`、`request`、`extraction`、`frames`、`failedFrames`。

关键约束：

- `evidenceID == frameID`。
- representative 使用 `_MAIN`；关键帧使用 `_Knn`。
- frame shotID 必须存在。
- `timeMs` 应位于所属镜头 final 区间；末端取样必须避免命中下一个镜头。
- `fileRef` 必须存在，除非该项在 failedFrames 中。
- 帧只用于人工校对和检索；UnifiedMLLM 请求中 `externalFrameExtraction` 必须为 false。

### 4.5 `raw/asr.json`

最低稳定字段：`service=asr`、`status`、`transcript.segments`。

关键约束：

- segments 按 startMs 排序。
- segment start 小于 end。
- segment 位于 analyzed range 内。
- speaker 和 confidence 可为 null。
- `status=complete` 时 transcript 必须存在；`skipped/failed` 时必须提供诊断信息或 manifest 记录。
- 镜头 speech 优先从 ASR 区间交集派生，模型 speech 仅作补充或比对。

### 4.6 `raw/music-flags.json`

顶层：`status`、`method`、`thresholds`、`speechGaps`、`textureEvents`、`musicIntervals`、`shots`。

每镜头 state：`music`、`silent`、`unknown`。

关键约束：

- overlap ratio 在 0..1。
- `stateTally` 与 shots 聚合一致。
- 本文件决定 BGM 是否存在；UnifiedMLLM 只描述 `bgmStyle`。
- 若 state=silent 且检测完成，可为 BGM presence 产生 `absent`。
- state=unknown 不得转成 absent。

### 4.7 `raw/unified-media.json`

顶层：`service`、`schemaFingerprint`、`request`、`retryPolicy`、`clips`、`batches`、`shotStatuses`、`status`。

`service` 固定 `unifiedAudioVideo`，`request.externalFrameExtraction` 固定 false，默认 transport 为 `videoDataURI`。

模型镜头字段组：

- `visual`：内容、主体、动作、场景、道具、景别、覆盖、角度、构图、观看关系、镜头感、运镜、运动强度、亮度、对比、光线、色温、主色、饱和度、景深、质感。
- `function`：素材形态、人物情绪、镜头语气。
- `audio`：语音、BGM 风格、音效。
- `components`：文字项和合成事件。
- `editing`：转场、连续性。
- `confidence`：visual/audio/editing/overall。

关键约束：

- 请求 shot 集合与成功响应 shot 集合必须完全一致，不允许漏项、重复或未知 ID。
- 每个 batch 的 shotIDs 与 response.shots 按 ID 对齐，不按数组位置盲目合并。
- `shotStatuses` 必须覆盖全部 clips。
- terminal=true 时不能有 pending。
- complete 时 failed/pending/permanentFailure 都为 0。
- partial 明确允许成功和永久失败并存。
- schemaFingerprint 由 prompt、schema、词表版本和解析版本组成。
- 模型输出“无”必须在 Observation 层转为 absent-claimed，raw 原值保留。

### 4.8 `raw/camera-motion.json`

顶层：`analysis`、`shots`。

cameraMovement 枚举：

```text
static, pan_left, pan_right, tilt_up, tilt_down,
zoom_in, zoom_out, roll, handheld, unknown, discontinuity
```

关键约束：

- Apple Vision 只证明图像运动，不证明真实摄影机运动或光学变焦。
- `neutralMotions` 保存中性信号，UI 文案不得夸大为确定摄影术语。
- sampleCount 过低应降低 confidence 或输出 unknown。
- cameraMovementCandidates 可多值，主值必须属于候选或为 unknown/discontinuity。
- 所有 metrics 保留单位说明。

### 4.9 `raw/quality-flags.json`

允许 flags：

- `画面模糊`
- `欠曝`
- `过曝`
- `音频削波`
- `黑场`
- `画面冻结`

关键约束：

- complete + high/medium confidence + flags=[] 才能形成确定性“未发现问题”。
- confidence=unknown 时不得把空 flags 解释为没有问题。
- audioStatus=absent 时不得报告音频削波。
- 测量值必须保留，阈值保存在文件顶层。

### 4.10 `raw/audio-energy.json`

每镜头 label：`静音`、`低`、`中`、`高`、`峰值`、`unknown`。

关键约束：

- 无音轨时 `hasAudio=false`，medianDb 为 null，label 应为 unknown 或由明确规则定义的静音；本实现默认 unknown，避免把无音轨和测得静音混为一谈。
- 有效音频时 minDb <= medianDb <= maxDb。
- frameCount 为 0 时不得产生伪造数值。
- thresholds 必须随产物保存。

### 4.11 `raw/story-blocks.json`

顶层：`status`、`boundarySource`、`gapMs`、`generatedAt`、`blocks`、`slots`。

受控集合以数据协议为准，包括：divisionAxis、primaryRole、informationRole、narrativeDensity、audienceReaction、visualIndependence、blockRelation、slotType。

关键约束：

- 每个 block 至少包含一个 shot。
- block shotIDs 必须存在且按镜头顺序排列。
- block start/end 从首尾镜头 final 边界派生。
- 默认模式下每个 shot 恰好属于一个 block，block 连续且不交叉。
- slot blockIDs 必须存在；默认模式下每个 block 至少属于一个 slot。
- `blockRelation` 中引用的 block 必须存在，且不能指向自身。
- `status=scaffold` 表示只有确定性聚块或不完整模型字段。
- `boundarySource=asr-gap` 为默认；model 为可选增强。

### 4.12 `style-profile.json`

顶层结构：`schemaVersion`、`id`、`createdAt`、`source`、`structure`、`pacing`、`style`、`structureRequirements`、`adoptionHints`、`discussionItems`、`asrTextStats`、`distillStatus`。

关键约束：

- `schemaVersion=2`。
- profile slot IDs 与 story slots 对齐。
- `L1.durationShare` 在 0..1，全部 slot 总和允许浮点容差。
- `L1.rangeSeconds` 由 story blocks 推导。
- `L3.shotIds` 必须存在，shotCount 与长度一致。
- `avgShotSeconds` 与镜头时长统计一致。
- 分布值在 0..1，同一分布总和允许舍入容差。
- hook/payoff 可为 null；不得为了填满 schema 伪造。
- 确定性聚合先写出；模型只补充主观层和建议。
- `distillStatus` 准确反映模型蒸馏，不影响确定性字段可用性。

## 5. 跨文件一致性

校验器必须实现以下连接：

```text
media.source.revisionID
  ├─ frame-evidence.request.sourceRevisionID
  ├─ unified-media.request.sourceRevisionID
  └─ style-profile.source.sourceRevision

shots.shots[].shotID
  ├─ audio-cuts.shots[].shotID
  ├─ frame-evidence.frames[].shotID
  ├─ music-flags.shots[].shotID
  ├─ unified-media.clips[].shotID
  ├─ camera-motion.shots[].shotID
  ├─ quality-flags.shots[].shotID
  ├─ audio-energy.shots[].shotID
  └─ story-blocks.blocks[].shotIDs[]

story-blocks.slots[].slotID
  └─ style-profile.structure.slots[].slotId
```

严格模式下，镜头级 complete 文件必须覆盖 shots.json 的全部镜头。partial/unavailable 文件允许不覆盖，但必须可解释。

## 6. 人工修正数据

HTML 不应直接覆盖 raw 文件。推荐新增 `corrections/*.json`：

```json
{
  "correctionVersion": 1,
  "documentType": "shotAnalysis",
  "sourceRevisionID": "a1b2c3d4e5f6",
  "changes": [
    {
      "entityID": "SH0001",
      "field": "visual.framing",
      "oldValue": "全景",
      "newValue": "中景",
      "state": "value",
      "verified": true,
      "changedAt": "2026-08-21T08:00:00Z",
      "actor": "human"
    }
  ]
}
```

渲染顺序：raw → resolver → corrections overlay → HTML。人工修改必须有版本和源 revision；源视频变化时文档状态变为 outdated，旧修正不自动套用到新的镜头 ID。

## 7. Schema 与迁移

- `schemas/` 中每个文件独立 schema。
- 契约测试必须验证文档示例和测试夹具。
- 对旧名称或旧字段采用显式迁移函数，不在读取器里堆叠隐式特殊分支。
- 迁移必须保留原文件备份或写到新路径。
- 任何破坏性字段变化必须提升 schema 版本并记录迁移。
