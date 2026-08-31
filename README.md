<p align="center">
  <img src="memoloupe-logo.png" width="70%" alt="MemoLoupe 标识">
</p>

<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="MemoLoupe 三阶段输出：镜头分析 shot-analysis.html、故事分析 story-analysis.html、风格档案 style-profile.json">
</p>

MemoLoupe 是把**参考视频拆解为可复刻契约**的分阶段媒体分析系统。它对视频做确定性检测（切镜、BGM、质量、运镜）与模型语义分析（镜头语言、故事结构、风格特征），产出三层产物，供人工校对后作为下游复刻的输入。

```text
Phase 1  镜头分析     raw/*.json + shot-analysis.html
Phase 2  故事分析     raw/story-blocks.json + story-analysis.html
Phase 3  风格档案     style-profile.json（schema v2）
```

## 为什么是三层

| 层 | 回答的问题 | 产物 |
|---|---|---|
| 镜头（Shot） | 画面里发生了什么、怎么拍的 | 镜头时间线、五态观察、证据引用、冲突复核 |
| 故事（Story） | 内容怎么组织、观众怎么被引导 | 故事块、插槽、叙事字段、块关系 |
| 风格（Profile） | 这套片子的结构与节奏怎么复刻 | 叙事/节奏/风格分布、复刻协商契约 |

每一层都坚持：

- **确定性优先**——切镜、BGM、质量、音频切点由检测器给出，模型不覆盖证据；
- **可追溯**——每个呈现值都能指回 raw 证据（`evidenceRefs`）；
- **失败可见**——模型不可用、跳过、降级都有显式状态，绝不伪装成结论；
- **人工校对闭环**——corrections overlay、review server、显式确认三闸门。

## 快速开始

```bash
# 安装
uv sync

# 可选：本地 ASR（FireRedVAD 人声切分 + MLX Whisper 识别，Apple Silicon）
uv sync --extra asr-local

# Phase 1：镜头分析（--dry-run 只跑确定性链路，不调用模型服务）
uv run memoloupe shot ./video.mp4 --output-dir ./out --dry-run

# 完整三阶段（Mock 服务演示，不发起网络请求）
uv run memoloupe shot  ./video.mp4 --output-dir ./out --mock-services
uv run memoloupe story   --output-dir ./out --allow-draft --mock-text-model
uv run memoloupe profile --output-dir ./out --mock-text-model

# 严格校验：schema + 跨文件 + HTML 语义
uv run memoloupe validate ./out --strict
```

真实服务配置见 [`.env.example`](.env.example)（`--env-file .env` 加载）；
本地 ASR 用 `asr.provider=local-fireredvad-mlx`（FireRedVAD + MLX Whisper，
需先 `uv sync --extra asr-local`）；
真实服务联调测试以 `MEMOLOUPE_RUN_REAL_SERVICE_TESTS=1` 显式启用。

## 能力一览

- **Phase 1 确定性检测**：ffprobe 探测、视觉硬切、音频六特征切点与音画关联、BGM 检测、音频能量、质量 flags、Apple Vision 运镜（macOS）
- **模型编排**：UnifiedMLLM v2 三组（visual / audio / function）批量并发、重试、`fallbackModel` 换模型重发、checkpoint 断点续跑；speech、内容摘要与运动强度由专属阶段解析
- **Phase 2/3 文本模型**：story 叙事填充与 profile 蒸馏——**只发送文本摘要，绝不发送视频或帧**
- **人工校对**：localhost review server、corrections 纯追加 overlay、`confirmed` 显式确认、`outdated` 优先级
- **校验闭环**：JSON Schema + 跨文件一致性 + HTML 语义，`validate --strict` 供 CI 门禁
- **调试能力**：`--skip STEP`、`--dry-run`、`--render-only`、`--strict`、`--max-shots`

## 文档

开发与扩展前必须按序阅读 `docs/`（完整清单见 [`AGENTS.md`](AGENTS.md)）：

- [`docs/00_REPRODUCTION_SPEC.md`](docs/00_REPRODUCTION_SPEC.md) — 产品边界与系统不变量
- [`docs/01_ARCHITECTURE_AND_MODULES.md`](docs/01_ARCHITECTURE_AND_MODULES.md) — 分层架构与模块职责
- [`docs/02_DATA_AND_STATE_CONTRACTS.md`](docs/02_DATA_AND_STATE_CONTRACTS.md) — 数据/状态契约实现约束
- [`docs/07_SOURCE_DATA_CONTRACT.md`](docs/07_SOURCE_DATA_CONTRACT.md) — 字段级数据来源契约
- [`docs/08_DEVELOPMENT_ROADMAP.md`](docs/08_DEVELOPMENT_ROADMAP.md) — 当前执行路线
- [`docs/09_COLLABORATION_BACKLOG.md`](docs/09_COLLABORATION_BACKLOG.md) — 协作开发 backlog

## 开发状态

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0–M3 | 契约、确定性 Phase 1、音频/视觉/模型编排、人工校对闭环 | ✅ |
| Phase 03 | 故事分析（scaffold、文本模型、story HTML/CLI/校验） | ✅ |
| Phase 04 | 风格档案（确定性聚合、模型蒸馏、CLI/校验） | ✅ |
| Phase 05 | 真实服务适配、完整词表、CLI 调试、配置可用性 | 🟡 已交付 05-01A~D / 05-02 / 05-03 框架 / 05-04 / 05-05 |
| 待外部输入 | 真实服务 smoke、黄金视频校准（A-001~A-007）、HTML 品牌/性能/发布 | ⏳ |

当前基线：`1125 passed, 6 skipped`（`uv run pytest -q`）。

## 产品边界

MemoLoupe 的输出止于 `shot-analysis.html`、`story-analysis.html`、`style-profile.json`。Story Spine 生成、用户素材匹配、自动粗剪、FCPXML 导出**不属于本仓库**——它们只能作为下游消费者验证数据是否足够，不进入核心流程。

## 测试

```bash
uv run pytest        # 全量：单元 + 契约 + 集成 + e2e
uv run memoloupe validate ./out --strict   # 产物严格校验
```
