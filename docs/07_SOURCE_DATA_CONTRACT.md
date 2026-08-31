# MemoLoupe 数据契约

本文档定义 MemoLoupe 拉片阶段的稳定机器数据契约，供后续代码复现、素材管理、快速混剪规划和 FCPXML 导出使用。

当前实现中，部分文件名仍沿用 `memoclip-lapian` 的内部命名，例如 `raw/shot-candidates.json`。本文档同时给出推荐稳定文件名与当前实现文件名的映射。

## 通用约定

- 契约版本：`1.0`
- 字符编码：UTF-8 JSON；Markdown 示例中的中文字段值必须按原样保留。
- 时间单位：毫秒，字段名以 `Ms` 结尾；秒只用于统计展示，字段名以 `Sec` 或 `Seconds` 结尾。
- 时间区间：`[startMs, endMs)`，包含起点，不包含终点。
- ID 格式：
  - 镜头：`SH0001`、`SH0002`，对应现有实现。
  - 音频切点：`AU0001`。
  - 帧证据：`F_<shotID>_<MAIN|K01|K02>`，例如 `F_SH0001_MAIN`。
  - 故事块：`B0001`。
  - 故事插槽：`S001`。
- 缺失字段处理：下游读取时应把缺失字段视为 schema 不完整或旧版本产物；如果字段存在但无法确认，必须显式写 `unknown`。
- `null` 与字段不存在的区别：
  - `null`：字段适用，但本次没有可用值，或模型蒸馏未返回。
  - 字段不存在：旧版本、未运行对应能力，或产物不符合当前契约。
- 置信度范围：
  - 离散置信度：`high`、`medium`、`low`、`unknown`。
  - 数值置信度：`0.0` 到 `1.0`，仅用于原始检测器指标。
- 取值状态：`value`、`absent`、`absent-claimed`、`unknown`、`unmapped`。
  - `value`：有具体取值。
  - `absent`：确定性检测器实测不存在。
  - `absent-claimed`：模型声称不存在，但没有确定性检测器背书。
  - `unknown`：无法确认。
  - `unmapped`：模型给了值，但不在受控词表内。
- `evidenceRefs` 格式：
  - JSON 指针：`raw/media.json#source.durationMs`
  - 数组项：`raw/shot-candidates.json#shots[0]`
  - 文件证据：`clips/SH0001.mp4`
  - 帧证据：`evidence/frames/F_SH0001_MAIN.jpg`
  - 多个证据用数组保存，不使用逗号拼接字符串。
- 坐标与比例：
  - 除非字段另有说明，比例范围为 `0.0` 到 `1.0`。
  - 颜色、构图、镜头语言等语义字段使用受控词表，受控词表来源为 `rules/vocabulary.json`。
- HTML 文件：
  - `shot-analysis.html` 与 `story-analysis.html` 是人工校对视图，不是下游混剪规划的主输入。
  - 下游系统应优先消费本文定义的 JSON 产物。

## 文件清单

| 稳定文件名 | 当前实现文件名 | 用途 |
|---|---|---|
| `media.json` | `raw/media.json` | 源视频元数据 |
| `shots.json` | `raw/shot-candidates.json` | 镜头边界与切点证据 |
| `audio-cuts.json` | `raw/audio-cuts.json` | 音频切点与音画边界事件 |
| `frame-evidence.json` | `raw/frame-evidence.json` | 代表帧与关键帧证据 |
| `asr.json` | `raw/asr.json` | 全片 ASR 转写 |
| `music-flags.json` | `raw/music-flags.json` | BGM 存在性检测 |
| `unified-media.json` | `raw/unified-media.json` | 统一音视频模型理解结果 |
| `camera-motion.json` | `raw/camera-motion.json` | Apple Vision 运镜候选 |
| `quality-flags.json` | `raw/quality-flags.json` | 画质/音质问题检测 |
| `audio-energy.json` | `raw/audio-energy.json` | 镜头响度 |
| `story-blocks.json` | `raw/story-blocks.json` | 故事块与故事插槽 |
| `style-profile.json` | `style-profile.json` | 复刻用结构与风格 Profile |

## media.json

### 用途

描述源视频的确定性媒体元数据，用于校验分析范围、复用缓存、生成证据引用和追踪源文件版本。

### 文件级结构

对象，顶层字段为 `source`。

### 字段

| 字段 | 类型 | 必填 | 允许值/单位 | 来源 | 说明 |
|---|---|---:|---|---|---|
| source.assetID | string | 是 | 文件 stem | ffprobe/系统 | 源素材稳定标识 |
| source.sourcePath | string | 是 | 绝对路径 | 系统 | 原视频路径 |
| source.revisionID | string | 是 | 12 位 sha256 前缀 | 系统 | 源文件内容版本 |
| source.durationMs | integer | 是 | ms | ffprobe | 视频总时长 |
| source.durationSec | number | 是 | sec | ffprobe | 视频总时长，展示用 |
| source.frameRate | number/null | 是 | fps | ffprobe | 平均帧率 |
| source.resolution.width | integer | 是 | px | ffprobe | 视频宽度 |
| source.resolution.height | integer | 是 | px | ffprobe | 视频高度 |
| source.aspectRatio | number/null | 是 | width/height | ffprobe | 画幅比例 |
| source.audioTracks | array | 是 | audioTrack[] | ffprobe | 音轨列表 |
| source.analyzedRange.startMs | integer | 是 | ms | 系统 | 分析起点 |
| source.analyzedRange.endMs | integer | 是 | ms | 系统 | 分析终点 |
| source.analysisCoverage | array | 是 | coverage[] | 系统 | 每类能力覆盖状态 |

### 完整示例

```json
{
  "source": {
    "assetID": "travel-reference",
    "sourcePath": "/Users/me/Videos/travel-reference.mp4",
    "revisionID": "a1b2c3d4e5f6",
    "durationMs": 61230,
    "durationSec": 61.23,
    "frameRate": 29.97003,
    "resolution": { "width": 1920, "height": 1080 },
    "aspectRatio": 1.777778,
    "audioTracks": [
      {
        "trackID": "1",
        "language": "unknown",
        "channels": 2,
        "sampleRate": 48000,
        "hasSpeech": "unknown",
        "hasMusic": "unknown",
        "hasEffects": "unknown"
      }
    ],
    "analyzedRange": { "startMs": 0, "endMs": 61230 },
    "analysisCoverage": [
      { "capability": "mediaMetadata", "status": "complete", "note": "ffprobe" }
    ]
  }
}
```

## shots.json

当前实现文件：`raw/shot-candidates.json`。

### 用途

描述检测镜头及音频对齐后的最终边界。它是所有镜头级分析的主索引。

### 文件级结构

对象，顶层字段为 `analysis`、`boundaries`、`suppressedBoundaries`、`shots`。

### 字段

| 字段 | 类型 | 必填 | 允许值/单位 | 来源 | 说明 |
|---|---|---:|---|---|---|
| analysis.method | string | 是 | `memoClipHardCutCandidateCuts` | 视觉检测 | 硬切检测方法 |
| analysis.fps | number | 是 | fps | 视觉检测 | 分析采样帧率 |
| analysis.sourceFps | number | 是 | fps | ffprobe | 源视频帧率 |
| analysis.durationMs | integer | 是 | ms | ffprobe | 源视频时长 |
| analysis.selectedBoundaryCount | integer | 是 | count | 视觉检测 | 有效切点数量 |
| analysis.rawCandidateCount | integer | 否 | count | 视觉检测 | 入选和被抑制候选总数 |
| analysis.suppressedBoundaryCount | integer | 否 | count | 视觉检测 | 被短段合并或 SSIM 拒绝的候选数 |
| boundaries | array | 是 | boundary[] | 视觉检测 | 候选画面切点 |
| suppressedBoundaries | array | 否 | boundary[] | 视觉检测 | 保留的被抑制原始证据，不形成 shot 边界 |
| shots | array | 是 | shot[] | 系统 | 最终镜头列表 |
| shot.shotID | string | 是 | `SH<4位数字>` | 系统 | 镜头唯一标识 |
| shot.sequenceIndex | integer | 是 | 从 1 开始 | 系统 | 时间顺序 |
| shot.detectedStartMs | integer | 是 | ms | 视觉检测 | 原始检测起点 |
| shot.detectedEndMs | integer | 是 | ms | 视觉检测 | 原始检测终点 |
| shot.finalStartMs | integer | 是 | ms | 边界对齐/人工校对 | 最终起点 |
| shot.finalEndMs | integer | 是 | ms | 边界对齐/人工校对 | 最终终点 |
| shot.durationMs | integer | 是 | ms | 系统 | `finalEndMs - finalStartMs` |
| shot.boundaryIn | object | 是 | boundaryRef | 视觉检测 | 入边界 |
| shot.boundaryOut | object | 是 | boundaryRef | 视觉检测 | 出边界 |
| shot.needsReview | boolean | 是 | true/false | 系统 | 是否需要人工复核 |
| boundary.timeSec | number | 是 | sec | 视觉检测 | 候选切点时间 |
| boundary.score | number | 是 | detector score | 视觉检测 | 切点分数，越低越像硬切 |
| boundary.histogramSimilarity | number | 是 | 0..1 | 视觉检测 | shots.v2 为 HSV 联合颜色直方图相交相似度 |
| boundary.edgeSimilarity | number | 是 | detector score | 视觉检测 | 边缘结构相似度 |
| boundary.contentDelta | number | 否 | >=0 | 视觉检测 | HSV 逐像素内容变化 |
| boundary.edgeDelta | number | 否 | >=0 | 视觉检测 | Sobel 边缘图变化 |
| boundary.adaptiveRatio | number | 否 | >=0 | 视觉检测 | 相对局部滑窗的变化比率 |
| boundary.ssim | number | 否 | -1..1 | 视觉检测 | 候选复核 SSIM |
| boundary.confidence | string | 是 | high/medium/low | 视觉检测 | 候选切点置信度 |
| boundary.selectionReason | string | 是 | `rawNegativeScore`/`adaptiveOutlier` | 视觉检测 | 入选原因 |

### 完整示例

```json
{
  "analysis": {
    "method": "memoClipHardCutCandidateCuts",
    "fps": 29.97003,
    "sourceFps": 29.97003,
    "sampleWidth": 128,
    "sampleHeight": 128,
    "durationMs": 61230,
    "selectedBoundaryCount": 1,
    "limitations": [
      "This detects hard-cut candidates; dissolves, fades, overlays, and fast subject motion still require visual confirmation."
    ]
  },
  "boundaries": [
    {
      "timeSec": 3.203,
      "score": -0.812,
      "histogramSimilarity": 0.41,
      "edgeSimilarity": 0.37,
      "brightness": 0.52,
      "brightnessDelta": 0.18,
      "frameIndex": 96,
      "type": "hardCutCandidate",
      "selectionReason": "rawNegativeScore",
      "confidence": "high"
    }
  ],
  "shots": [
    {
      "shotID": "SH0001",
      "sequenceIndex": 1,
      "detectedStartMs": 0,
      "detectedEndMs": 3203,
      "finalStartMs": 0,
      "finalEndMs": 3203,
      "durationMs": 3203,
      "boundaryIn": {
        "type": "sourceStart",
        "confidence": "high",
        "metric": null
      },
      "boundaryOut": {
        "type": "hardCutCandidate",
        "confidence": "high",
        "metric": { "timeSec": 3.203, "score": -0.812 }
      },
      "needsReview": true
    }
  ]
}
```

## audio-cuts.json

### 用途

描述最终混音中的音频突变候选，并把它们与画面镜头边界对齐。该文件只判断同步切、声音连续、无法归因；不再输出 J-cut/L-cut。

### 文件级结构

对象，顶层字段为 `status`、`analysis`、`boundaries`、`shots`。

### 字段

| 字段 | 类型 | 必填 | 允许值/单位 | 来源 | 说明 |
|---|---|---:|---|---|---|
| status | string | 是 | complete/unavailable | 音频检测 | 分析状态 |
| analysis.method | string | 是 | `audioFeatureNoveltyHardCutCandidates` | 音频检测 | 方法名 |
| analysis.syncToleranceMs | integer | 是 | ms | 音频检测 | 同步切容差 |
| analysis.associationWindowMs | integer | 是 | ms | 音频检测 | 音画关联窗口 |
| boundaries | array | 是 | audioBoundary[] | 音频检测 | 音频切点候选 |
| boundary.audioBoundaryID | string | 是 | `AU<4位数字>` | 系统 | 音频切点 ID |
| boundary.timeMs | integer | 是 | ms | 音频检测 | 音频切点时间 |
| boundary.score | number | 是 | detector score | 音频检测 | 音频突变强度 |
| boundary.confidence | string | 是 | high/medium | 音频检测 | 音频切点置信度 |
| boundary.featureDeltas | object | 是 | number map | 音频检测 | 特征变化量 |
| shots | array | 是 | shotBoundary[] | 系统 | 每个镜头入/出边界的音画关系 |
| shot.shotID | string | 是 | `SH0001` | 系统 | 镜头 ID |
| shot.boundaryIn.classification | string | 是 | 见下 | 音频检测 | 入边界分类 |
| shot.boundaryOut.classification | string | 是 | 见下 | 音频检测 | 出边界分类 |
| boundary*.labelZh | string | 是 | 中文短语 | 系统 | 人类可读标签 |
| boundary*.visualTimeMs | integer | 是 | ms | shots.json | 画面边界时间 |
| boundary*.audioTimeMs | integer/null | 否 | ms | audio-cuts.json | 匹配音频切点时间 |
| boundary*.offsetMs | integer/null | 否 | ms | 系统 | `audioTimeMs - visualTimeMs` |

`classification` 允许值：

| 值 | 含义 |
|---|---|
| `sourceStart` | 片头 |
| `sourceEnd` | 片尾 |
| `synchronizedCut` | 音画同步切 |
| `pictureCutAudioContinuous` | 画面切，混音层未测到音频切点 |
| `audioBoundaryUndetermined` | 测到音频切点，但同步窗外无法归因 |
| `unavailable` | 音频不可用 |

### 完整示例

```json
{
  "status": "complete",
  "analysis": {
    "method": "audioFeatureNoveltyHardCutCandidates",
    "analysisSampleRate": 16000,
    "frameMs": 20,
    "threshold": 8.0,
    "syncToleranceMs": 100,
    "associationWindowMs": 500,
    "selectedBoundaryCount": 1
  },
  "boundaries": [
    {
      "timeMs": 3200,
      "score": 12.41,
      "confidence": "high",
      "featureDeltas": { "rmsDb": 7.2, "zeroCrossingRate": 0.04 },
      "audioBoundaryID": "AU0001"
    }
  ],
  "shots": [
    {
      "shotID": "SH0001",
      "boundaryIn": {
        "classification": "sourceStart",
        "labelZh": "片头（音画同时开始）",
        "visualTimeMs": 0,
        "confidence": "high"
      },
      "boundaryOut": {
        "classification": "synchronizedCut",
        "labelZh": "音画同步切（偏差 -3 ms）",
        "visualTimeMs": 3203,
        "audioTimeMs": 3200,
        "offsetMs": -3,
        "confidence": "high",
        "audioBoundaryID": "AU0001",
        "audioBoundaryScore": 12.41
      }
    }
  ]
}
```

## frame-evidence.json

### 用途

描述每个镜头的代表帧和关键帧证据。帧证据用于人工校对、故事切分辅助和后续视觉检索索引，不替代真实视频 clip。

### 文件级结构

对象，顶层字段为 `status`、`request`、`extraction`、`frames`、`failedFrames`。

### 字段

| 字段 | 类型 | 必填 | 允许值/单位 | 来源 | 说明 |
|---|---|---:|---|---|---|
| status | string | 是 | complete/failed | 系统 | 抽帧状态 |
| request.sourceRevisionID | string/null | 是 | revisionID | media.json | 源文件版本 |
| request.inputVideo | string | 是 | 绝对路径 | 系统 | 抽帧输入视频 |
| request.inputCacheKey | string | 是 | string | 系统 | proxy 或 original 缓存键 |
| request.width | integer | 是 | px | 系统 | 输出帧宽度 |
| frames | array | 是 | frame[] | ffmpeg | 已抽取帧 |
| frame.evidenceID | string | 是 | `F_SH0001_MAIN` | 系统 | 证据 ID |
| frame.frameID | string | 是 | 同 evidenceID | 系统 | 帧 ID |
| frame.shotID | string | 是 | `SH0001` | shots.json | 所属镜头 |
| frame.frameType | string | 是 | representative/keyframe | 系统 | 帧类型 |
| frame.timeMs | integer | 是 | ms | shots.json | 帧所在时间 |
| frame.range.startMs | integer | 是 | ms | 系统 | 证据时间起点 |
| frame.range.endMs | integer | 是 | ms | 系统 | 证据时间终点 |
| frame.fileRef | string | 是 | 相对路径 | 系统 | 图片路径 |
| frame.quality | string | 是 | usable/unknown | 系统 | 帧可用性 |
| frame.summary | string | 是 | 文本 | 系统 | 备注 |
| failedFrames | array | 是 | failure[] | 系统 | 抽取失败项 |

### 完整示例

```json
{
  "status": "complete",
  "request": {
    "sourceRevisionID": "a1b2c3d4e5f6",
    "inputVideo": "/outputs/travel/media/lapian-proxy.mp4",
    "inputCacheKey": "proxy-a1b2",
    "width": 640,
    "jpegQuality": 5
  },
  "extraction": {
    "mode": "auto",
    "workerCount": 4,
    "cachedFrames": 0
  },
  "frames": [
    {
      "evidenceID": "F_SH0001_MAIN",
      "frameID": "F_SH0001_MAIN",
      "shotID": "SH0001",
      "frameType": "representative",
      "timeMs": 1602,
      "range": { "startMs": 1602, "endMs": 1602 },
      "fileRef": "evidence/frames/F_SH0001_MAIN.jpg",
      "quality": "usable",
      "summary": "agent visual review required"
    }
  ],
  "failedFrames": []
}
```

## asr.json

### 用途

保存全片 ASR 转写。故事块切分、BGM 检测、镜头语音字段和后续素材叙事理解都会引用它。

### 文件级结构

对象。当前实现允许服务返回扩展字段；稳定契约要求至少包含 `service`、`status`、`transcript.segments`。

### 字段

| 字段 | 类型 | 必填 | 允许值/单位 | 来源 | 说明 |
|---|---|---:|---|---|---|
| service | string | 是 | `asr` | 系统 | 服务类型 |
| status | string | 是 | complete/skipped/failed | ASR | 分析状态 |
| transcript.text | string | 否 | 原文 | ASR | 全文转写 |
| transcript.segments | array | 是 | asrSegment[] | ASR | 分句转写 |
| segment.startMs | integer | 是 | ms | ASR | 起点 |
| segment.endMs | integer | 是 | ms | ASR | 终点 |
| segment.text | string | 是 | 原文 | ASR | 语音文本 |
| segment.speaker | string/null | 否 | speaker ID | ASR | 说话人 |
| segment.confidence | number/string/null | 否 | 0.0-1.0 或 high/medium/low | ASR | 转写置信度 |

### 完整示例

```json
{
  "service": "asr",
  "status": "complete",
  "transcript": {
    "text": "今天我们从机场出发。",
    "segments": [
      {
        "startMs": 820,
        "endMs": 2460,
        "text": "今天我们从机场出发。",
        "speaker": "SPEAKER_0",
        "confidence": 0.92
      }
    ]
  }
}
```

## music-flags.json

### 用途

用确定性音频信号判断每个镜头是否有背景音乐。该文件负责“有没有 BGM”，模型只负责在确认有音乐时描述 `bgmStyle`。

### 文件级结构

对象，顶层字段为 `status`、`method`、`thresholds`、`speechGaps`、`textureEvents`、`musicIntervals`、`shots`。

### 字段

| 字段 | 类型 | 必填 | 允许值/单位 | 来源 | 说明 |
|---|---|---:|---|---|---|
| status | string | 是 | complete | 音频检测 | 分析状态 |
| method | string | 是 | 文本 | 音频检测 | 方法说明 |
| stateTally | object | 是 | count map | 系统 | music/silent/unknown 统计 |
| speechGaps | array | 是 | gap[] | ASR+音频检测 | 语音间隙判定 |
| textureEvents | array | 是 | textureEvent[] | 音频检测 | 音质地突变事件 |
| musicIntervals | array | 是 | interval[] | 音频检测 | 实测音乐区间 |
| shots | array | 是 | shotMusic[] | 音频检测 | 每镜头 BGM 状态 |
| shot.shotID | string | 是 | `SH0001` | shots.json | 镜头 ID |
| shot.startMs | integer | 是 | ms | shots.json | 镜头起点 |
| shot.endMs | integer | 是 | ms | shots.json | 镜头终点 |
| shot.state | string | 是 | music/silent/unknown | 音频检测 | BGM 状态 |
| shot.confidence | string | 是 | high/medium/unknown | 音频检测 | 置信度 |
| shot.basis | string | 是 | 中文说明 | 音频检测 | 判定依据 |
| shot.musicOverlapRatio | number | 是 | 0.0-1.0 | 音频检测 | 与音乐区间重叠比例 |
| shot.silentOverlapRatio | number | 是 | 0.0-1.0 | 音频检测 | 与静音区间重叠比例 |
| shot.events | array | 是 | textureEvent[] | 音频检测 | 镜头内突变事件 |

### 完整示例

```json
{
  "status": "complete",
  "method": "numpy STFT：语音间隙电平/低频判定 + 谱平坦度突变事件",
  "stateTally": { "music": 1, "silent": 0, "unknown": 0 },
  "thresholds": {
    "sampleRate": 22050,
    "musicLevelDb": -18.0,
    "musicBassEnergy": 150.0,
    "silentLevelDb": -22.0
  },
  "speechGaps": [
    {
      "startSec": 0.0,
      "endSec": 0.82,
      "state": "music",
      "measurements": {
        "medianLevelDb": -12.5,
        "medianBassEnergy": 242.1,
        "medianFlatness": 0.31
      }
    }
  ],
  "textureEvents": [
    {
      "atSec": 0.42,
      "kind": "textureRise",
      "flatnessDelta": 0.19,
      "label": "音质地变粗糙（疑似音乐/打击乐进入）"
    }
  ],
  "musicIntervals": [
    { "startSec": 0.0, "endSec": 3.2, "origin": "gapAnchor" }
  ],
  "shots": [
    {
      "shotID": "SH0001",
      "startMs": 0,
      "endMs": 3203,
      "state": "music",
      "confidence": "high",
      "basis": "镜头有 100% 时长落在实测音乐区间内。",
      "musicOverlapRatio": 1.0,
      "silentOverlapRatio": 0.0,
      "events": []
    }
  ]
}
```

## unified-media.json

### 用途

保存统一音视频模型对每个镜头的内容、镜头语言、声音、文字组件和剪辑语义的结构化理解。该文件是镜头语义层的主数据源，但所有模型断言都需要保留来源和可复核性。

### 文件级结构

对象，顶层字段为 `schemaVersion`、`service`、`schemaFingerprint`、`request`、`retryPolicy`、`clips`、`batches`、`shotStatuses`、`status`。

### 字段

| 字段 | 类型 | 必填 | 允许值/单位 | 来源 | 说明 |
|---|---|---:|---|---|---|
| schemaVersion | integer | 是 | `2` | 系统 | unified-media 契约版本 |
| service | string | 是 | `unifiedAudioVideo` | 系统 | 服务类型 |
| schemaFingerprint | string | 是 | 16 位 hash | 系统 | prompt/schema 指纹 |
| request.model | string | 是 | model name | 系统 | 主模型 |
| request.fallbackModel | string/null | 否 | model name | 系统 | 备用模型 |
| request.clipTransport | string | 是 | `videoDataURI` | 系统 | clip 传输方式 |
| request.batchSize | integer | 是 | count | 系统 | 每批 clip 数 |
| request.concurrency | integer | 是 | count | 系统 | 并发批次数 |
| request.externalFrameExtraction | boolean | 是 | false | 系统 | 统一模型不读取外部抽帧 |
| request.videoFPS | number | 是 | fps | 系统 | 模型输入 fps |
| request.mediaResolution | string | 是 | default/low/high | 系统 | 模型输入分辨率 |
| request.sourceRevisionID | string/null | 是 | revisionID | media.json | 源文件版本 |
| request.shortClipPolicy | object | 是 | policy | 系统 | 短 clip 补齐策略 |
| clips | array | 是 | clip[] | ffmpeg | 每个镜头证据 clip 和模型输入 clip |
| clip.shotID | string | 是 | `SH0001` | shots.json | 镜头 ID |
| clip.startMs | integer | 是 | ms | shots.json | clip 起点 |
| clip.endMs | integer | 是 | ms | shots.json | clip 终点 |
| clip.durationMs | integer | 是 | ms | 系统 | 原始 clip 时长 |
| clip.file | string | 是 | 相对路径 | ffmpeg | 原始证据 clip |
| clip.modelFile | string | 是 | 相对路径 | ffmpeg | 模型输入 clip |
| clip.modelDurationMs | integer | 是 | ms | 系统 | 模型输入时长 |
| clip.modelNormalization | object/null | 否 | normalization | 系统 | proxy/补帧/缩放信息 |
| batches | array | 是 | batch[] | 系统/模型 | 批量请求结果 |
| batch.batchID | string | 是 | `B0001` | 系统 | 批次 ID |
| batch.shotIDs | array | 是 | string[] | 系统 | 批次镜头 |
| batch.status | string | 是 | complete/dryRun/failed | 系统 | 批次状态 |
| batch.response.shots | array | 条件必填 | modelShot[] | 模型 | 模型返回镜头 |
| shotStatuses | object | 是 | shotID -> status | 系统 | succeeded/pending/permanent_failure |
| status | string | 是 | complete/partial/running/dryRun/skipped/failed | 系统 | 文件整体状态 |

### modelShot 字段

| 字段 | 类型 | 必填 | 允许值/单位 | 来源 | 说明 |
|---|---|---:|---|---|---|
| shotID | string | 是 | `SH0001` | 系统/模型 | 镜头 ID，必须覆盖请求集合 |
| visual.subjects | string | 是 | 短中文短语/unknown/无 | 模型 | 主体 |
| visual.actions | string | 是 | 短中文短语/unknown/无 | 模型 | 动作 |
| visual.setting | string | 是 | 短中文短语/unknown | 模型 | 场景 |
| visual.props | string | 是 | 短中文短语/unknown/无 | 模型 | 道具 |
| visual.framing | string | 是 | 受控词表 | 模型 | 景别，可用 ` → ` 表示变化 |
| visual.cameraAngle | string | 是 | 受控词表 | 模型 | 摄影机角度 |
| visual.composition | string | 是 | 受控词表 | 模型 | 构图 |
| visual.viewpoint | string | 是 | 受控词表 | 模型 | 叙事观看关系 |
| visual.perceivedLensFeel | string | 是 | 受控词表 | 模型 | 感知透视感，不作为焦段事实 |
| visual.cameraMovement | string | 是 | 受控词表 | 模型 | 运镜现象 |
| visual.brightness | string | 是 | 受控词表 | 模型 | 亮度 |
| visual.contrast | string | 是 | 受控词表 | 模型 | 对比度 |
| visual.lightingSource | string | 是 | 自然光/人工光/混合光 | 模型 | 光源来源，不混入亮度 |
| visual.perceivedColorTemperature | string | 是 | 冷/中性/暖 | 模型 | 感知色温 |
| visual.dominantColor | string | 是 | 短中文短语 | 模型 | 主色 |
| visual.saturation | string | 是 | 受控词表 | 模型 | 饱和度 |
| visual.depthOfField | string | 是 | 受控词表 | 模型 | 景深 |
| visual.imageTexture | string | 是 | 受控词表 | 模型 | 成像质感 |
| function.sourceMedium | string | 是 | 受控词表 | 模型 | 素材形态 |
| function.subjectEmotion | string | 是 | 受控词表 | 模型 | 人物情绪 |
| function.shotTone | string | 是 | 受控词表 | 模型 | 镜头语气 |
| audio.bgmStyle | string | 是 | 风格短语/unknown | 模型 | BGM 风格，不回答有无 |
| audio.soundEvents | string | 是 | 短中文短语/unknown/无 | 模型 | 可听声音事件 |
| components.texts | array | 是 | textItem[] | 模型 | 画面文字/后期文字 |
| textItem.textContent | string | 是 | 原文 | 模型 | 文字内容 |
| textItem.textType | string | 是 | 受控词表 | 模型 | 文字类型 |
| textItem.textStyle | string | 是 | 短中文短语 | 模型 | 文字样式 |
| textItem.textAnimation | string | 是 | 受控词表 | 模型 | 文字动画 |
| components.nonTextOverlayEvents | string | 是 | 短中文短语/unknown/无 | 模型 | 非文字后期图层/合成事件 |
| confidence.visual | string | 是 | high/medium/low/unknown | 模型 | 视觉字段自评 |
| confidence.audio | string | 是 | high/medium/low/unknown | 模型 | 声音字段自评 |
| confidence.function | string | 是 | high/medium/low/unknown | 模型 | 功能语义字段自评 |

以下是产品 Observation 字段而非 modelShot 字段：

- `visual.contentSummary`：由 subjects/actions/setting/props 拼接，source=`aggregate`；
- `audio.speech`：只由 ASR 生成；
- `visual.movementIntensity`：只由 camera-motion 生成；
- `editing.transition`：当前仅把 shots 的 `hardCutCandidate` 派生为“硬切”；
- `editing.continuity`：v2 暂不生成，后续必须由相邻镜头关系分析器负责。

### 完整示例

```json
{
  "schemaVersion": 2,
  "service": "unifiedAudioVideo",
  "schemaFingerprint": "86ad19c6afbf1a02",
  "request": {
    "model": "mimo-v2.5",
    "fallbackModel": "mimo-v2.5",
    "clipTransport": "videoDataURI",
    "batchSize": 4,
    "concurrency": 10,
    "externalFrameExtraction": false,
    "videoFPS": 10.0,
    "mediaResolution": "default",
    "sourceRevisionID": "a1b2c3d4e5f6",
    "shortClipPolicy": {
      "minimumDurationMs": 800,
      "recoveryMinimumDurationMs": 2000,
      "recoveryWidth": 720
    }
  },
  "retryPolicy": {
    "maxRetries": 3,
    "fallbackFromBatchToSingleShot": true,
    "checkpointAfterEachRequest": true
  },
  "clips": [
    {
      "shotID": "SH0001",
      "startMs": 0,
      "endMs": 3203,
      "durationMs": 3203,
      "file": "clips/SH0001.mp4",
      "modelFile": "clips/model-proxy/SH0001-a1b2.mp4",
      "modelDurationMs": 3203,
      "modelNormalization": {
        "strategy": "shared-audio-proxy",
        "cacheKey": "proxy-a1b2",
        "file": "clips/model-proxy/SH0001-a1b2.mp4"
      }
    }
  ],
  "batches": [
    {
      "batchID": "B0001",
      "shotIDs": ["SH0001"],
      "status": "complete",
      "response": {
        "shots": [
          {
            "shotID": "SH0001",
            "visual": {
              "subjects": "旅行者",
              "actions": "拖行李走动",
              "setting": "机场出发大厅",
              "props": "行李箱",
              "framing": "全景",
              "cameraAngle": "平视",
              "composition": "居中",
              "viewpoint": "第三人称观察",
              "perceivedLensFeel": "广角感",
              "cameraMovement": "跟",
              "brightness": "明亮",
              "contrast": "中",
              "lightingSource": "自然光",
              "perceivedColorTemperature": "冷",
              "dominantColor": "蓝白",
              "saturation": "中",
              "depthOfField": "深景深",
              "imageTexture": "清晰"
            },
            "function": {
              "sourceMedium": "实拍素材",
              "subjectEmotion": "期待",
              "shotTone": "轻快"
            },
            "audio": {
              "bgmStyle": "轻快 电子",
              "soundEvents": "行李轮声"
            },
            "components": {
              "texts": [
                {
                  "textContent": "DAY 1",
                  "textType": "标题",
                  "textStyle": "白色粗体",
                  "textAnimation": "淡入"
                }
              ],
              "nonTextOverlayEvents": "无"
            },
            "confidence": {
              "visual": "medium",
              "audio": "medium",
              "function": "medium"
            }
          }
        ]
      }
    }
  ],
  "shotStatuses": { "SH0001": "succeeded" },
  "completedShots": 1,
  "failedShots": 0,
  "pendingShots": 0,
  "permanentFailureShots": 0,
  "terminal": true,
  "status": "complete"
}
```

## camera-motion.json

### 用途

保存 Apple Vision 对每个镜头的图像运动测量结果。它提供运镜候选和运动强度证据，不能单独证明光学变焦、后期缩放或真实摄影机运动。

### 文件级结构

对象，顶层字段为 `analysis`、`shots`。

### 字段

| 字段 | 类型 | 必填 | 允许值/单位 | 来源 | 说明 |
|---|---|---:|---|---|---|
| analysis.method | string | 是 | Vision method | Apple Vision | 方法名 |
| analysis.durationMs | integer | 是 | ms | AVFoundation | 源时长 |
| analysis.sampleFps | number | 是 | fps | 系统 | 采样频率 |
| analysis.maximumFramesPerShot | integer | 是 | count | 系统 | 每镜头最多帧数 |
| shots | array | 是 | motionShot[] | Apple Vision | 每镜头运动结果 |
| shot.shotID | string | 是 | `SH0001` | shots.json | 镜头 ID |
| shot.sequenceIndex | integer | 是 | 从 1 开始 | shots.json | 顺序 |
| shot.startMs | integer | 是 | ms | shots.json | 起点 |
| shot.endMs | integer | 是 | ms | shots.json | 终点 |
| shot.cameraMovement | string | 是 | static/pan_left/pan_right/tilt_up/tilt_down/zoom_in/zoom_out/roll/handheld/unknown/discontinuity | Apple Vision | 主候选 |
| shot.cameraMovementCandidates | array | 是 | string[] | Apple Vision | 候选列表 |
| shot.movementIntensity | string | 是 | static/low/medium/high/unknown | Apple Vision | 运动强度 |
| shot.confidence | string | 是 | high/medium/low/unknown | Apple Vision | 置信度 |
| shot.neutralMotions | array | 是 | string[] | Apple Vision | 中性图像运动信号 |
| shot.metrics | object | 是 | number map | Apple Vision | 聚合指标 |
| shot.evidence | object | 是 | evidence | Apple Vision | 原始帧级证据 |

### 完整示例

```json
{
  "analysis": {
    "method": "Apple Vision VNTrackHomographicImageRegistrationRequest + VNTrackOpticalFlowRequest",
    "durationMs": 61230,
    "sourceWidth": 1920,
    "sourceHeight": 1080,
    "sampleFps": 2.0,
    "maximumFramesPerShot": 12,
    "maximumImageDimension": 960,
    "opticalFlowEnabled": true,
    "units": "pixel-equivalent motion at the sampled, preferred-transform image size"
  },
  "shots": [
    {
      "shotID": "SH0001",
      "sequenceIndex": 1,
      "startMs": 0,
      "endMs": 3203,
      "durationMs": 3203,
      "sampleCount": 6,
      "cameraMovement": "pan_right",
      "cameraMovementCandidates": ["pan_right"],
      "movementIntensity": "medium",
      "confidence": "medium",
      "neutralMotions": ["horizontal_frame_shift"],
      "needsReview": true,
      "metrics": {
        "medianFrameShiftX": -8.2,
        "medianFrameShiftY": 0.4,
        "medianScale": 1.001,
        "medianRotationDegrees": 0.02,
        "motionScore": 8.2,
        "geometricMotionScore": 8.2,
        "homographyFrameCount": 5,
        "opticalFlowFrameCount": 5
      },
      "evidence": {
        "method": "Apple Vision VNTrackHomographicImageRegistrationRequest + VNTrackOpticalFlowRequest",
        "discontinuityFrameIndexes": [],
        "frames": []
      }
    }
  ]
}
```

## quality-flags.json

### 用途

保存确定性画质/音质问题检测结果。`flags` 为空表示所有阈值都没触发，是确定性结论；若 `confidence=unknown`，表示该镜头视频采样不足，不能声称完全没问题。

### 文件级结构

对象，顶层字段为 `status`、`method`、`thresholds`、`shots`。

### 字段

| 字段 | 类型 | 必填 | 允许值/单位 | 来源 | 说明 |
|---|---|---:|---|---|---|
| status | string | 是 | complete | ffmpeg | 分析状态 |
| method | string | 是 | 文本 | ffmpeg | 方法说明 |
| audioStatus | string | 是 | complete/absent/failed | ffmpeg/ffprobe | 音频扫描状态 |
| thresholds | object | 是 | number map | 系统 | 阈值 |
| shots | array | 是 | qualityShot[] | ffmpeg | 每镜头质量 |
| shot.shotID | string | 是 | `SH0001` | shots.json | 镜头 ID |
| shot.startMs | integer | 是 | ms | shots.json | 起点 |
| shot.endMs | integer | 是 | ms | shots.json | 终点 |
| shot.flags | array | 是 | 中文标签[] | ffmpeg | 质量问题 |
| shot.confidence | string | 是 | high/medium/unknown | ffmpeg | 置信度 |
| shot.measurements | object | 是 | number map | ffmpeg | 采样统计 |

`flags` 允许值：`画面模糊`、`欠曝`、`过曝`、`音频削波`、`黑场`、`画面冻结`。

### 完整示例

```json
{
  "status": "complete",
  "method": "ffmpeg blurdetect + signalstats + astats + blackdetect + freezedetect",
  "audioStatus": "complete",
  "flaggedShotCount": 1,
  "shotCount": 1,
  "thresholds": {
    "videoSampleFps": 2.0,
    "blurFlagThreshold": 11.0,
    "underexposedYAVG": 40.0,
    "overexposedYAVG": 215.0
  },
  "shots": [
    {
      "shotID": "SH0001",
      "startMs": 0,
      "endMs": 3203,
      "flags": ["画面模糊"],
      "confidence": "high",
      "measurements": {
        "videoSampleCount": 6,
        "audioSampleCount": 160,
        "medianBlur": 12.7,
        "medianYAVG": 120.4
      }
    }
  ]
}
```

## audio-energy.json

### 用途

保存每个镜头的响度标签和 RMS 统计，用于节奏/能量曲线、素材筛选和混剪强弱匹配。

### 文件级结构

对象，顶层字段为 `source`、`durationMs`、`sampleRate`、`hasAudio`、`thresholds`、`shots`。

### 字段

| 字段 | 类型 | 必填 | 允许值/单位 | 来源 | 说明 |
|---|---|---:|---|---|---|
| source | string | 是 | 路径 | 系统 | 源视频 |
| durationMs | integer | 是 | ms | ffprobe | 时长 |
| sampleRate | integer | 是 | Hz | ffprobe | 音频采样率 |
| hasAudio | boolean | 是 | true/false | ffprobe | 是否有音轨 |
| thresholds | object | 是 | dB | 系统 | 响度阈值 |
| shots | array | 是 | energyShot[] | 音频检测 | 每镜头响度 |
| shot.shotID | string | 是 | `SH0001` | shots.json | 镜头 ID |
| shot.label | string | 是 | 静音/低/中/高/峰值/unknown | 音频检测 | 响度标签 |
| shot.medianDb | number/null | 是 | dB | 音频检测 | 中位 RMS |
| shot.frameCount | integer | 是 | count | 音频检测 | 有效音频帧数 |
| shot.minDb | number | 条件必填 | dB | 音频检测 | 最低 RMS |
| shot.maxDb | number | 条件必填 | dB | 音频检测 | 最高 RMS |

### 完整示例

```json
{
  "source": "/Users/me/Videos/travel-reference.mp4",
  "durationMs": 61230,
  "sampleRate": 48000,
  "hasAudio": true,
  "thresholds": {
    "silent": -60.0,
    "low": -40.0,
    "medium": -25.0,
    "high": -12.0
  },
  "shots": [
    {
      "shotID": "SH0001",
      "label": "中",
      "medianDb": -28.4,
      "frameCount": 160,
      "minDb": -38.2,
      "maxDb": -18.1
    }
  ]
}
```

## story-blocks.json

### 用途

把连续镜头聚合成故事块，并进一步聚合成故事插槽。这个文件表达参考片的叙事逻辑、信息推进、观众反应和块间关系，是后续“复刻情绪与叙事结构”的核心输入。

### 文件级结构

对象，顶层字段为 `status`、`boundarySource`、`gapMs`、`generatedAt`、`blocks`、`slots`。

### 字段

| 字段 | 类型 | 必填 | 允许值/单位 | 来源 | 说明 |
|---|---|---:|---|---|---|
| status | string | 是 | complete/scaffold | 系统/模型 | 故事分析状态 |
| boundarySource | string | 是 | model/asr-gap | 系统 | 边界来源 |
| gapMs | integer | 是 | ms | 系统 | ASR 停顿阈值 |
| generatedAt | string | 是 | ISO-8601 | 系统 | 生成时间 |
| blocks | array | 是 | storyBlock[] | 模型/系统 | 故事块 |
| slots | array | 是 | storySlot[] | 模型 | 故事插槽 |
| block.storyBlockID | string | 是 | `B0001` | 系统 | 故事块 ID |
| block.shotIDs | array | 是 | shotID[] | shots.json | 覆盖镜头 |
| block.startMs | integer | 是 | ms | 派生 | 块起点 |
| block.endMs | integer | 是 | ms | 派生 | 块终点 |
| block.boundaryBasis | string | 否 | 文本 | 模型 | 边界切分依据 |
| block.boundary | object | 是 | boundarySignal | 系统 | 入边界确定性信号 |
| block.blockTitle | string | 否 | <=12 字 | 模型 | 块标题 |
| block.divisionAxis | string | 是 | 见下 | 模型 | 划分维度 |
| block.divisionRationale | string | 是 | 文本 | 模型 | 划分理由 |
| block.primaryRole | string | 是 | 见下 | 模型 | 结构角色 |
| block.coreContent | string | 是 | 文本 | 模型 | 核心内容 |
| block.informationRole | string | 是 | 多选顿号分隔 | 模型 | 信息作用 |
| block.narrativeDensity | string | 是 | 高/中/低/unknown | 模型 | 叙事密度 |
| block.audienceReaction | string | 是 | 见下 | 模型 | 预期观众反应 |
| block.visualIndependence | string | 是 | 见下 | 模型 | 画面独立性 |
| block.blockRelation | string | 是 | 关系词 + 指向 | 模型 | 与前后块/非线性块关系 |
| block.relationReason | string | 是 | 文本 | 模型 | 关系理由 |
| slot.slotID | string | 是 | `S001` | 系统 | 插槽 ID |
| slot.slotType | string | 是 | 见下，可多选 | 模型 | 宏观叙事功能 |
| slot.slotTitle | string | 是 | <=15 字 | 模型 | 插槽标题 |
| slot.blockIDs | array | 是 | storyBlockID[] | 模型 | 覆盖故事块 |
| slot.slotRationale | string | 是 | 文本 | 模型 | 聚合理由 |

### 受控集合

| 字段 | 允许值 |
|---|---|
| divisionAxis | `主题/话题`、`行动/任务`、`场景/时空`、`情绪/语气`、`人物/主体`、`unknown` |
| primaryRole | `hook`、`context`、`promise`、`problem`、`development`、`proof`、`turn`、`payoff`、`resolution`、`custom`、`unknown` |
| informationRole | `建立背景`、`推进新信息`、`解释原因`、`演示步骤`、`得出结论`、`重复强调`、`转移话题`，可多选；`unknown` 仅作 scaffold/模型失败占位，不参与多选组合 |
| narrativeDensity | `高`、`中`、`低`、`unknown` |
| audienceReaction | `好奇/想看下去`、`共鸣/代入`、`意外/反转`、`娱乐/好笑`、`获得信息/学到东西`、`建立信任/认同`、`无强烈反应（信息过场）`、`unknown` |
| visualIndependence | `静音也能看懂`、`需要声音辅助`、`没有声音完全看不懂`、`unknown` |
| blockRelation | 逻辑推进：`铺垫`、`延续`、`对比`、`转折`、`兑现`、`解决`；时间结构：`闪回`、`预叙`、`平行`、`嵌套`、`循环` |
| slotType | `开场引入`、`背景铺垫`、`行动展开`、`冲突转折`、`深度剖析`、`高潮兑现`、`总结升华`、`结尾收束`、`custom`，可多选 |

### 完整示例

```json
{
  "status": "complete",
  "boundarySource": "model",
  "gapMs": 1200,
  "generatedAt": "2026-08-21T08:00:00+00:00",
  "blocks": [
    {
      "storyBlockID": "B0001",
      "shotIDs": ["SH0001", "SH0002"],
      "startMs": 0,
      "endMs": 6400,
      "boundaryBasis": "片头先用机场和行李建立旅程开始。",
      "boundary": {
        "level": "start",
        "signal": "片头",
        "label": "片头"
      },
      "blockTitle": "机场出发",
      "divisionAxis": "行动/任务",
      "divisionRationale": "这两个镜头都在交代出发行动。",
      "primaryRole": "hook",
      "coreContent": "旅行从机场出发，建立即将展开的旅程期待。",
      "informationRole": "建立背景、推进新信息",
      "narrativeDensity": "中",
      "audienceReaction": "好奇/想看下去",
      "visualIndependence": "静音也能看懂",
      "blockRelation": "铺垫 → B0002",
      "relationReason": "先建立出发场景，后续进入目的地体验。"
    }
  ],
  "slots": [
    {
      "slotID": "S001",
      "slotType": "开场引入",
      "slotTitle": "旅程启动",
      "blockIDs": ["B0001"],
      "slotRationale": "片头以出发动作和地点建立旅拍主题。"
    }
  ]
}
```

## style-profile.json

### 用途

把镜头表和故事表蒸馏成复刻协商契约。它不要求用户素材 1:1 复刻语义内容，而是复刻叙事逻辑、情绪曲线、节奏模式、视觉风格分布和素材前提。

### 文件级结构

对象，顶层字段为 `schemaVersion`、`id`、`createdAt`、`source`、`structure`、`pacing`、`style`、`structureRequirements`、`adoptionHints`、`discussionItems`、`asrTextStats`、`distillStatus`。

### 字段

| 字段 | 类型 | 必填 | 允许值/单位 | 来源 | 说明 |
|---|---|---:|---|---|---|
| schemaVersion | integer | 是 | 当前为 2 | 系统 | Profile schema 版本 |
| id | string | 是 | `profile-<revision>` | 系统 | Profile ID |
| createdAt | string | 是 | ISO-8601 | 系统 | 生成时间 |
| source.videoTitle | string/null | 是 | 文本 | media.json | 源视频标题/assetID |
| source.videoPath | string/null | 是 | 路径 | media.json | 源视频路径 |
| source.durationSeconds | number | 是 | sec | media.json | 时长 |
| source.shotAnalysisPath | string | 是 | 路径 | 系统 | 镜头 HTML |
| source.storyAnalysisPath | string | 是 | 路径 | 系统 | 故事 HTML |
| source.sourceRevision | string/null | 是 | revisionID | media.json | 源版本 |
| structure.slots | array | 是 | profileSlot[] | 聚合/模型 | 复刻插槽序列 |
| structure.hook | object/null | 是 | layeredRole | 聚合/模型 | hook 定位 |
| structure.payoff | object/null | 是 | layeredRole | 聚合/模型 | payoff 定位 |
| structure.turns | array | 是 | turn[] | story-blocks.json | 转折点 |
| structure.nonLinearDevices | array | 是 | nonlinear[] | story-blocks.json | 非线性结构 |
| structure.expectationChains | array | 是 | chain[] | story-blocks.json | 期望链 |
| pacing.shotDuration | object | 是 | seconds stats | 聚合 | 镜头时长分布 |
| pacing.densityCurve | array | 是 | slot density[] | 聚合 | 叙事密度曲线 |
| pacing.slotPacing | array | 是 | slot pacing[] | 聚合 | 插槽节奏 |
| pacing.audioBoundaryBySlot | array | 是 | slot boundary[] | 聚合 | 插槽音画边界对齐 |
| pacing.musicAlignment | string | 是 | 文本/unknown | 聚合 | 音乐进出对齐 |
| style | object | 是 | distributions | 聚合 | 视觉/声音/剪辑风格统计 |
| structureRequirements | array | 是 | requirement[] | 模型 | 用户素材硬前提 |
| adoptionHints | object/null | 是 | hints | 模型 | 复刻建议 |
| discussionItems | array | 是 | question[] | 模型 | 复刻前澄清问题 |
| asrTextStats | object | 是 | stats | ASR 聚合 | 口播文本统计 |
| distillStatus | string | 是 | complete/empty/unavailable/skipped | 系统 | 模型蒸馏状态 |

### profileSlot 字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| slotId | string | 是 | `S001` |
| L1.types | array | 是 | 插槽宏观类型 |
| L1.functionalTitle | string/null | 是 | 抽象功能命名，不含具体内容 |
| L1.narrativeFunction | string | 是 | `setup`、`progression`、`complication`、`resolution`、`reflection` |
| L1.durationShare | number | 是 | 该插槽占全片比例 |
| L1.rangeSeconds | array | 是 | `[startSec, endSec]` |
| L1.intendedReaction | string/null | 是 | 目标观众反应 |
| L1.minBlocks | integer | 是 | 最小故事块数量建议 |
| L2.carriage | string/null | 是 | 承载方式 |
| L2.pattern | string/null | 是 | 节奏/表现模式 |
| L2.referenceContent | string | 是 | 参考片具体内容摘要 |
| L3.shotIds | array | 是 | 对应镜头 |
| L3.shotCount | integer | 是 | 镜头数 |
| L3.avgShotSeconds | number | 是 | 平均镜头时长 |

### 完整示例

```json
{
  "schemaVersion": 2,
  "id": "profile-a1b2c3d4e5f6",
  "createdAt": "2026-08-21T08:00:00+00:00",
  "source": {
    "videoTitle": "travel-reference",
    "videoPath": "/Users/me/Videos/travel-reference.mp4",
    "durationSeconds": 61.23,
    "platform": null,
    "formType": null,
    "shotAnalysisPath": "/outputs/travel/shot-analysis.html",
    "storyAnalysisPath": "/outputs/travel/story-analysis.html",
    "sourceRevision": "a1b2c3d4e5f6"
  },
  "structure": {
    "slots": [
      {
        "slotId": "S001",
        "L1": {
          "types": ["开场引入"],
          "functionalTitle": "期待型旅程开场",
          "narrativeFunction": "setup",
          "durationShare": 0.12,
          "rangeSeconds": [0.0, 7.35],
          "intendedReaction": "好奇/想看下去",
          "minBlocks": 1
        },
        "L2": {
          "carriage": "出发动线承载",
          "pattern": "快切建立情绪",
          "referenceContent": "用机场、行李和出发动作建立旅程期待。"
        },
        "L3": {
          "shotIds": ["SH0001", "SH0002"],
          "shotCount": 2,
          "avgShotSeconds": 3.68
        }
      }
    ],
    "hook": {
      "L1": {
        "atSeconds": 0.0,
        "slotId": "S001",
        "blockId": "B0001"
      },
      "L2": {
        "form": "用出发动作直接制造旅程期待",
        "referenceContent": "旅行从机场出发。"
      },
      "L3": {
        "shotIds": ["SH0001", "SH0002"]
      }
    },
    "payoff": null,
    "turns": [],
    "nonLinearDevices": [],
    "expectationChains": [
      {
        "kind": "铺垫",
        "fromSlot": "S001",
        "toSlot": "S002",
        "evidence": {
          "blockId": "B0001",
          "relation": "铺垫 → B0002"
        }
      }
    ]
  },
  "pacing": {
    "shotDuration": { "mean": 2.18, "p50": 1.72 },
    "densityCurve": [{ "slotId": "S001", "density": "中" }],
    "slotPacing": [{ "slotId": "S001", "shotCount": 2, "avgShotSeconds": 3.68 }],
    "audioBoundaryBySlot": [{ "slotId": "S001", "boundaryAligned": true }],
    "musicAlignment": "music starts near opening"
  },
  "style": {
    "transitions": { "硬切": 0.84, "淡入淡出": 0.16 },
    "framing": { "全景": 0.42, "中景": 0.35, "特写": 0.23 },
    "cameraMovement": { "跟": 0.33, "固定": 0.28 },
    "textOverlay": { "coverage": 0.18 },
    "bgm": { "coverage": 0.72 },
    "voiceMix": { "speechCoverage": 0.31 },
    "hostedCoverage": 0.12
  },
  "structureRequirements": [
    {
      "slotId": "S001",
      "requirementType": "evidence",
      "description": "需要能表达出发或进入旅程状态的素材。",
      "minEvidence": "至少 2 个可用镜头"
    }
  ],
  "adoptionHints": {
    "strengths": ["用行动而不是旁白快速建立主题"],
    "cautions": ["不要强求用户拥有同一机场或同一地点素材"],
    "suggestedDefault": "L1+L2"
  },
  "discussionItems": [
    {
      "id": "q-1",
      "layer": "L2",
      "category": "applicability",
      "question": "用户素材里是否有能承担出发/进入状态的镜头？",
      "options": [
        { "id": "a", "label": "有明确出发镜头" },
        { "id": "b", "label": "用抵达或开场环境替代" }
      ],
      "impactLevel": "preference",
      "defaultIfUnanswered": "用最强环境建立镜头替代出发动作"
    }
  ],
  "asrTextStats": {
    "segmentCount": 12,
    "characterCount": 186,
    "speechDurationMs": 19040
  },
  "distillStatus": "complete"
}
```

## 下游消费建议

MemoLoupe 输出不应该让素材管理做 1:1 语义复刻。推荐消费顺序如下：

1. 用 `style-profile.json.structure.slots` 获取叙事阶段、情绪目标、时长占比和最低素材需求。
2. 用 `story-blocks.json.blocks` 获取块级信息推进、观众反应和关系链。
3. 用 `shots.json`、`audio-energy.json`、`music-flags.json`、`camera-motion.json` 获取节奏、能量和运动曲线。
4. 用 `unified-media.json` 获取镜头语义与视觉风格，但只作为软匹配特征。
5. 用 `quality-flags.json` 对候选素材做硬过滤：只过滤明显不可用素材，不因语义不一致而丢弃可承载同等情绪/叙事功能的素材。

复刻匹配的核心单位应是“故事插槽 affordance”，不是“原片镜头语义”。例如旅拍参考片中的“机场出发”可以被用户素材中的“上车、推门、走进街区、打开地图、到达酒店”替代，只要它承担相同的开场引入、期待建立和节奏启动功能。
