---
name: memoloupe
description: MemoLoupe 拉片分析 CLI：镜头切分、故事块、风格档案、切点关系与审片工作台。当用户要求分析参考视频（拉片/分镜/镜头切分/故事结构/风格档案/审片工作台/校对确认），或提到 memoloupe、shot-analysis.html、style-profile、切点关系时使用。
---

# MemoLoupe 拉片分析

MemoLoupe 把参考视频转化为可追溯的结构化拉片数据：镜头切分、音频/视觉
证据、故事块、风格档案、相邻镜头（pair）关系与帧级审片工作台。JSON 是
机器主契约，`shot-analysis.html` 是人工校对视图。

## 前提

```bash
# 安装（Homebrew tap）
brew install memobloom/memoloupe/memoloupe

# 模型服务（语义阶段必需；未配置时确定性阶段仍可用，语义显式降级）
memoloupe connect add mimo    # 或 qwen，交互式录入 baseUrl/model/apiKey
memoloupe connect switch mimo
memoloupe connect status
```

依赖 ffmpeg/ffprobe（Homebrew 自动带入）。macOS 14+ 才有 Apple Vision
运动分析；未安装本地 ASR extra 时自动走已配置的远程 provider。

## 核心命令

```bash
# 阶段 1+2：镜头 + 故事（最常用入口）
memoloupe shot <input.mp4> --output-dir <out-dir>

# 常用变体
memoloupe shot <input> --output-dir <out> --start-ms 0 --end-ms 60000   # 片段
memoloupe shot <input> --output-dir <out> --skip-story                  # 只跑镜头
memoloupe shot <input> --output-dir <out> --force build_shot_relations  # 重跑某步
memoloupe shot <input> --output-dir <out> --skip detect_music           # 跳过可选步骤

# 阶段 3：风格档案（要求镜头分析已 confirmed）
memoloupe profile --output-dir <out>

# 校验（任何写入后必须执行；--strict 用于交付门槛）
memoloupe validate <out-dir> --strict

# 人工校对服务（localhost）与离线校对导入
memoloupe review <out-dir>
memoloupe import-corrections <out-dir> --file corrections.json

# 服务状态与配置自检
memoloupe connect list | status | test | switch mimo|qwen
memoloupe config
```

## 产物速查（`<out-dir>/`）

| 文件 | 内容 |
|---|---|
| `raw/media.json` | 源探测：时长/分辨率/音轨/analyzedRange |
| `raw/shots.json` | 镜头边界（detected 与 final 双轨）与切分依据 |
| `raw/frame-evidence.json` | 每镜头代表帧 + fileRef |
| `raw/audio-cuts.json` / `audio-energy.json` / `music-flags.json` | 音频切点、响度、音乐状态 |
| `raw/unified-media.json` | 视觉语义（模型；未配置时 skipped） |
| `raw/camera-motion.json` / `quality-flags.json` | 运动与质量标记 |
| `raw/review-timeline.json` | 逐帧 PTS 索引 + 波形 envelope（审片台用） |
| `raw/shot-relations.json` | 相邻 pair 确定性指标 + 可选语义 + 复核状态 |
| `raw/story-blocks.json` | 故事块（shot --story-only / 合并流程） |
| `style-profile.json` | 风格档案（Phase 3） |
| `shot-analysis.html` | 审片工作台（浏览器直接打开 / `memoloupe review`） |
| `corrections/` | 人工校对记录（追加历史，不覆盖） |

## 强制规则（违反即产出不可信）

1. **时间用整数毫秒，区间一律 `[startMs, endMs)`**；展示字段除外。
2. `detectedStartMs/detectedEndMs` 永不修改；人工/音频对齐只能改 final 边界。
3. 检测边界与最终边界必须同时保留，不得互相覆盖。
4. 模型声称"没有"只能是 `absent-claimed`，绝不能写 `absent`；
   `verified` 是人工核实状态，与五态取值相互独立。
5. 每个呈现给用户的分析值必须带 `evidenceRefs`（指向 raw 证据）。
6. 写 JSON 必须走 `ArtifactStore`（临时文件 + 原子替换）；不要手写脚本
   直接改 `raw/*.json`——用 `import-corrections` 走校对通道。
7. 模型/ASR/Apple Vision 不可用是显式状态（unavailable/skipped/unknown），
   不是空数据；不得把降级伪装成"确认没有"。
8. 任何写操作之后运行 `memoloupe validate <out> --strict`，0 error 才算完成。

## 典型工作流

```bash
# 首次分析（自动：探测→切镜→音频→帧→clip→语义→审片索引→pair→渲染→校验→故事）
memoloupe shot video/ref.mp4 --output-dir out/ref
memoloupe validate out/ref --strict

# 人工校对循环
memoloupe review out/ref          # 浏览器校对、保存 corrections
memoloupe shot <input> --output-dir out/ref   # 重跑使 final 边界/校对生效

# 校对确认后再出风格档案
memoloupe profile --output-dir out/ref
memoloupe validate out/ref --strict
```

## 故障排查

- `ffmpeg 未找到`：`brew install ffmpeg`。
- 语义全 skipped/unknown：`memoloupe connect status` 检查 active provider；
  `memoloupe connect test` 验证凭据；切 `mimo`/`qwen` 后重跑
  `--force unified_media_analysis --force build_shot_relations`。
- 校验报 evidenceRef 不可解析：不要手工改 raw 文件；用
  `import-corrections` 或修正来源步骤后 `--force` 重跑。
- `--strict` 报 partial：某模型步骤未完成，按报告里的步骤名
  `--force <step>` 断点续跑（其余步骤命中指纹直接复用）。
- 长片首跑慢：语义与 pair 语义按指纹缓存；重跑只补缺失步骤。
