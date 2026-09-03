# MemoLoupe 后续开发路线（M3–M5）

状态：当前执行路线  
更新日期：2026-08-25  
基线提交：`eb8b8e2`（M2：音频与智能分析）  
适用对象：开发型 AI、GSD planner/executor、人工开发者
## 1. 使用说明

本文档是 M2 之后的当前路线，覆盖 `docs/05_TESTING_AND_ACCEPTANCE.md` 早期版本中的 M3–M5 命名。稳定字段和系统不变量仍按 `AGENTS.md` 的规范优先级执行。

本文按 GSD phase/plan 结构编排，可作为未来 `$gsd-import --from docs/08_DEVELOPMENT_ROADMAP.md` 的输入。正式导入 GSD 前应先初始化 `.planning/PROJECT.md`、`REQUIREMENTS.md`、`ROADMAP.md` 和 `STATE.md`。

开发纪律：

1. Schema、契约和测试先行。
2. 先写失败测试，再做最小实现。
3. 每个 plan 完成后运行相关单元、契约和集成测试。
4. 每个 phase 完成后运行全量 `uv run pytest -q`。
5. 生成样例产物并运行 `uv run memoloupe validate <output-dir> --strict`。
6. 所有新增决策和校准参数写入 `docs/06_DECISIONS_AND_ASSUMPTIONS.md`。

## 2. 已验证基线

截至 2026-08-26：

- M0+M1 已交付：可执行契约、ArtifactStore、校验器、确定性 Phase 1、shot HTML。
- M2 已交付：音频六特征、BGM、ASR、Apple Vision、三组 UnifiedMLLM、checkpoint 和降级路径。
- M3 已交付：人工校对闭环（corrections overlay、review server、import-corrections）。
- Phase 03 已交付（03-01~03-04）：review reason 机器语义、确定性 story scaffold、
  文本模型编排、story HTML/CLI/校验闭环。
- Phase 04 已交付（04-01~04-03）：确定性 profile 聚合、模型蒸馏、CLI 与校验闭环。
- Phase 05-01A 已交付：story/profile 真实文本模型 CLI 注入、显式跳过/strict 开关、`.env.example`。
- Phase 05-01B 已交付：UnifiedMLLM fallback 真实重发（`fallbackModel` 换模型重发，
  产物记录 fallbackUsed/fallbackFailed）。
- Phase 05-01C 已交付：ASR provider 选择（openai-json / openai-multipart 适配器）。
- Phase 05-01D 已交付：真实服务 opt-in 测试框架、脱敏 fixture、日志/配置脱敏审计。
- Phase 05-02 已交付：完整受控词表、版本化缓存失效、迁移护栏。
- Phase 05-03 框架已交付：黄金标注格式、校准指标（core/calibration.py）、
  opt-in 校准测试（真实视频/标注到达后启用）。
- Phase 05-04 已交付：`--skip`/`--dry-run`/`--render-only`/`--strict`/`--max-shots`。
- Phase 05-05 已交付：`--env-file` 加载、`memoloupe config` 脱敏自检。
- 本地 ASR provider 已交付（2026-08-28，决策 D-045）：
  `asr.provider=local-fireredvad-mlx`，FireRedVAD 人声切分 +
  MLX whisper-large-v3-turbo 识别，optional extra `asr-local`。
- connect-first CLI 已交付（2026-09-02，决策 D-053~D-055，分支
  `feat/connect-cli`）：`memoloupe connect` 命令组（add/status/test/
  switch/remove/list）、connections.json 连接存储 + Keychain 凭据、
  qwen/mimo provider 注册表、shot/story/profile 自动叠加 active provider、
  `asr.provider=auto` 本地优先路由；`shot --help` 分发缺陷已修复。
  `memoloupe login`（官方托管服务）保留为未来项，不在本期。
- shot+story 合并流程已交付（2026-09-03，决策 D-056）：`memoloupe shot`
  默认链式执行故事分析，主流程收敛为 `shot`（分析）+ `profile`（导出）
  两条命令；独立 `memoloupe story` 保留为校对后重跑入口。
- MiMo ASR 已交付（2026-09-03，决策 D-057）：`asr.provider=mimo-chat`
  （chat/completions + input_audio，窗口切片）；mimo provider 声明 ASR
  能力，`connect add mimo` 后管道自动走 mimo-v2.5-asr。mimo-v2.5-tts
  已实测可用，但不属于产品边界，不集成。
- Qwen ASR 已交付（2026-09-03，决策 D-058）：`asr.provider=qwen-chat`
  （`WindowedChatASR` 共享基类 + `QwenChatASR`）；qwen 默认模型更新为
  media/text=`qwen3.8-flash`、asr=`qwen3-asr-flash`，两模型已用真实 key
  实测可用。注意 `qwen-audio-asr-flash` 该名称不存在（404）。
- 全量测试：`1125 passed, 6 skipped`。
- motion-effects 已交付（2026-09-03，决策 D-061）：Phase 1 新增确定性 raw
  artifact `raw/motion-effects.json`（运动复刻候选检测：曲线变速区域
  low/high/impact + 关键帧 position/scale/shake/exposure 候选，全部
  needsVisualConfirmation=true；不覆盖 camera-motion/quality-flags；第一版
  不进 style-profile），含 schema v1、`--skip detect_motion_effects` 与
  dry-run 显式 skipped stub、cross-artifact shotID/evidenceRefs 校验、
  shot HTML “运动复刻候选/待视觉确认”清单。执行目标见
  `docs/superpowers/plans/2026-09-03-motion-effects-development-goal.md`。
- BGM `music.v2` 已修复连续演唱漏检：全轨 STFT 始终运行并融合 ASR gap
  anchor，使用谱平坦度约束、短缺口合并和持续静音门槛；`disney.MP4`
  只读重算由 0 个 music 镜头提升为 59 个（决策 D-049）。

已关闭的路线分歧：

- BGM 存在性只由 `music-flags.json` 的确定性检测负责；UnifiedMLLM 只输出 `bgmStyle`。
- UnifiedMLLM v2 使用 visual/audio/function 三组内部执行；speech、contentSummary、movementIntensity 与 transition 由专属 Resolver/确定性证据提供；下游只消费合并后的稳定 `unified-media.json`。
- aligned shots 的 base/aligned 指纹复用已经实现，并由 `test_aligned_run_is_fully_reusable_on_second_run` 覆盖。

## 3. 里程碑总览

| Phase | 目标 | 前置 | 完成状态 |
|---|---|---|---|
| 03 | Phase 1 收尾并交付完整故事分析 | M2 | ✅ 已完成（03-01~03-04） |
| 04 | 交付 schema v2 风格档案 | Phase 03 | ✅ 已完成（04-01~04-03） |
| 05 | 真实服务、完整词表、真实样例校准与体验收尾 | Phase 03/04 | 05-01A~D、05-02、05-03 框架、05-04、05-05、05-07（motion-effects v1）已完成；05-03 校准与 05-06 待外部输入 |

## 4. Phase 03：渲染收尾与故事分析

### Phase 目标

完成 Phase 1 的 review reason 呈现，并交付从确认镜头数据到 `story-blocks.json`、`story-analysis.html` 的完整纵向链路。Phase 2 的文本模型不得接收视频或帧。

### Phase 成功标准

- resolver 冲突理由显示在 shot 镜头列，并能被 HTML 校验器验证。
- 默认按 ASR gap 确定性聚块；模型失败时仍输出 `status=scaffold`。
- 文本模型只补充叙事字段和 slot，不修改确定性镜头集合。
- `memoloupe story` 可运行、可复用 checkpoint、可显式降级。
- `raw/story-blocks.json` 通过 schema 和跨文件严格校验。
- `story-analysis.html` 离线可打开，具备五态、证据和人工校对语义。
- 全量测试通过，并新增 Phase 2 E2E。

### 03-01：Phase 1 review reason 与缓存收尾

类型：refactor/test  
依赖：无  
状态：已完成（2026-08-25，871 tests 全绿）

必须实现：

- [x] `render/shot_html.py` 改用 `build_observations_with_review()`。
- [x] 将 review reasons 与 `shots.json.needsReview` 合并为镜头级展示状态。
- [x] 镜头列显示可读冲突理由；模型内容必须 HTML escape。
- [x] 为 `data-review-reasons` 或等价机器属性定义稳定 HTML 语义。
- [x] 扩展 HTML 校验器，验证 needs-review 与 reasons 的一致性。
- [x] 增加 resolver→render→validate 回归测试。
- [x] detect_shots 同时接受 base/aligned 指纹。
- [x] aligned shots 第二次运行全部复用且不重复移动 final 边界。

主要文件：

- `src/memoloupe/analysis/resolvers.py`
- `src/memoloupe/render/shot_html.py`
- `src/memoloupe/validate/html_contract.py`
- `tests/unit/test_shot_html.py`
- `tests/unit/test_html_contract.py`
- `tests/unit/test_shot_pipeline.py`

验收：

- Vision 与模型运镜冲突时，页面镜头列出现明确理由。
- 无冲突时不显示空 warning 容器。
- existing 749 tests 不回归。

### 03-02：确定性 Story Scaffold

类型：feature/test
依赖：03-01 可并行，但 Phase 结束前必须完成
状态：已完成（2026-08-25，`analysis/story_pipeline.py`，905 tests 全绿）

新增 `analysis/story_pipeline.py`，先实现不依赖文本模型的故事脚手架。

必须实现：

- [x] 读取并校验 `media.json`、`shots.json`、`asr.json`、`unified-media.json`。
- [x] 构造镜头文本摘要：shotID、时间、派生 contentSummary、subjects/actions/setting/props、ASR speech、文字、确定性转场和必要确定性信号。
- [x] 摘要对象不得包含 clip、帧 Data URI、源视频二进制或模型代理路径。
- [x] ASR segments 按时间排序，按 `gapMs`（默认 1200）创建停顿段。
- [x] 实现 `segment_of(start_ms, end_ms)`；首镜头必须强制创建首 block。
- [x] 遍历镜头，停顿段变化时创建新 block。
- [x] block 的 start/end 从首尾镜头 final 边界派生。
- [x] 无 ASR 时采用保守单块 scaffold，不自行猜测视觉故事边界。
- [x] scaffold 填充合法 unknown/default 叙事字段，状态为 `scaffold`、boundarySource 为 `asr-gap`。
- [x] checkpoint 指纹包含 shots、ASR、gapMs、实现版本。

建议接口：

```python
@dataclass(frozen=True)
class StoryAnalysisRequest:
    output_dir: Path
    gap_ms: int = 1200
    allow_draft: bool = False
    text_service: TextModelService | None = None
    force: frozenset[str] = frozenset()

class StoryAnalysisPipeline:
    def run(self, request: StoryAnalysisRequest) -> PipelineReport: ...
```

测试至少覆盖：

- 首镜头开块。
- gap 小于、等于、大于 1200ms 的边界行为。
- ASR segment 跨镜头。
- 无 ASR/ASR failed/skipped。
- 单镜头、无对白镜头、连续对白、多段对白。
- block 时间、shotIDs 顺序和全量覆盖。
- 同指纹重跑复用。

### 03-03：Story 文本模型编排

类型：feature/test
依赖：03-02
状态：已完成（2026-08-25，`services/text_model.py` + `analysis/story_prompts.py` + `analysis/story_pipeline.py` 编排，923 tests 全绿）

新增通用文本模型端口和 Mock/OpenAI-compatible 实现，服务于 story 和后续 profile 蒸馏。

建议模块：

- `src/memoloupe/services/text_model.py`
- `src/memoloupe/analysis/story_prompts.py`

必须实现：

- [x] `TextModelService` 协议接受结构化请求、返回原始 JSON 文本。
- [x] Mock 支持成功、非法 JSON、漏 block、未知 block、暂时失败和永久失败（`MockTextModelService` 按调用序号编排 str/Exception）。
- [x] OpenAI-compatible 复用 `services/base.py` 的 HTTP、鉴权和脱敏逻辑（`OpenAICompatibleTextModel` → `/chat/completions`）。
- [x] story prompt 只含文本摘要；测试断言 payload 不含视频/Data URI。
- [x] 返回字段覆盖 block title、division、role、content、information role、density、reaction、visual independence、relation。
- [x] slot 返回 slotType、title、blockIDs、rationale。
- [x] Schema 校验、受控词表归一化、ID 集合闭合（`parse_model_result` + 合并后 `validate_artifact`；词表常量单一事实源在 `story_prompts`）。
- [x] 模型不得新增、删除或重排 shot；默认不得修改确定性 block 边界（模型返回 shotIDs/startMs/endMs 不一致即整体判不合规；合并只复制叙事白名单字段）。
- [x] 模型不可用或返回不合规时保留 scaffold，不能丢失候选 blocks。
- [x] 每次成功请求后 checkpoint（`checkpoints/story-blocks-model.json`）；失败重跑只补缺失项（指纹命中跳过请求）。

附带契约收紧：`schemas/story-blocks.json` 增加 `blockTitle.maxLength=12`、`slotTitle.maxLength=15`（docs/03 §3.3 要求 schema 与后处理双重检查）。

状态规则：

- 所有叙事字段成功且引用闭合：`status=complete`。
- 无模型、部分失败或非法响应：`status=scaffold`。
- `boundarySource=model` 只在未来显式启用模型边界模式时使用，默认保持 `asr-gap`。

### 03-04：Story HTML、CLI 与校验闭环

类型：feature/test
依赖：03-02、03-03
状态：已完成（2026-08-25，968 tests 全绿）

新增：

- `src/memoloupe/render/story_html.py`
- `templates/story-analysis.html`
- `src/memoloupe/cli/story_analysis.py`

必须实现：

- [x] Story 时间线、block、slot、镜头覆盖和关系视图。
- [x] 复用 Phase 1 的五态、confidence、evidenceRefs、verified 语义。
- [x] story-block DOM 只出现在 storyAnalysis 文档。
- [x] 资源离线、路径相对、无外链脚本。
- [x] `memoloupe story --output-dir DIR` 替换 `_cmd_not_implemented`。
- [x] 默认要求 shot analysis 可用；`--allow-draft` 显式允许未确认输入。
- [x] validate 命令同时检查 story JSON 和 story HTML。
- [x] 跨文件校验 block→shot、slot→block、relation→block。
- [x] 生成最小样例和完整 Mock 样例。

实现要点（决策记录见 docs/06 D-033）：

- story HTML 的叙事字段以五态单元格呈现：scaffold 占位（枚举 unknown、
  自由文本空串）统一为 `state=unknown`，模型填充值为 `state=value`、
  source=`textModel`、evidenceRefs 指向 `raw/story-blocks.json#blocks[N].field`；
- 人工校对复用 corrections overlay：entityID 取 storyBlockID/slotID，
  文档状态由 `corrections/storyAnalysis.json` 推导（outdated 优先）；
- `html_contract` 对 storyAnalysis 增加结构检查（至少一个 story-block、
  块头必带 data-story-block-id/data-shot-ids/data-start-ms/data-end-ms、
  每 block 至少一个可追溯证据单元格）与 strict 数据一致性检查
  （block ID 集合、shotIDs、起止时间与 raw/story-blocks.json 对齐）；
- CLI 草稿门禁：默认要求 shots.json+media.json 合法（退出码 3）；
  2026-08-25 验收修复后收紧为还要求 `shotAnalysis` corrections 显式
  `confirmed`；`--allow-draft` 是开发/调试绕过；`--mock-text-model`
  提供 callable mock 文本模型（按 prompt 块 ID 回填，供演示/测试）。
- story HTML strict 校验会解析 `data-evidence-refs` 到实际文件/JSON 节点；
  complete story 必须让每个 block 至少属于一个 slot。
- story 重写会把既有 `style-profile.json` 归档到 `checkpoints/outdated/`，
  避免陈旧 Phase 3 产物污染当前 strict 校验。

Phase 03 E2E（`tests/e2e/test_phase2_e2e.py`）：

```text
Phase 1 fixture/output
→ story scaffold
→ Mock text model enrichment
→ raw/story-blocks.json
→ story-analysis.html
→ memoloupe validate --strict
```

## 5. Phase 04：风格档案

### Phase 目标

从镜头和故事契约生成 `style-profile.json` schema v2。先确定性聚合，再模型蒸馏；模型失败不得影响确定性统计。

### Phase 成功标准

- `analysis/profile_aggregate.py` 不导入或调用任何模型服务。
- `memoloupe profile` 在无模型模式下仍生成合法 profile。
- 结构、节奏、分布和文本统计可由 raw 产物重新计算一致。
- 模型只补主观字段，不覆盖计数、时长、ID 和分布。
- `style-profile.json` 通过 schema v2 和跨文件严格校验。

### 04-01：确定性 Profile Aggregate

类型：feature/test  
依赖：Phase 03
状态：已完成（2026-08-25，`analysis/profile_aggregate.py`，1041 tests 全绿）

新增 `src/memoloupe/analysis/profile_aggregate.py`，设计为纯函数友好模块。

必须计算：

- [x] slot 序列、时间范围、durationShare、minBlocks。
- [x] L3 shotIds、shotCount、avgShotSeconds。
- [x] 全片镜头时长 mean、p50；可附加 p10/p90。
- [x] densityCurve、slotPacing。
- [x] audioBoundaryBySlot 和 musicAlignment。
- [x] transition、framing、lighting、cameraMovement 分布。
- [x] textOverlay coverage、BGM coverage、speech/voiceMix coverage。
- [x] hostedCoverage。
- [x] ASR segmentCount、characterCount、speechDurationMs。
- [x] turns、nonLinearDevices、expectationChains。

当前默认规则（决策记录见 docs/06 D-034/D-035）：

- 风格分布按镜头数计权；unknown/unmapped 不入分布。
- coverage 按分析范围内的时间并集计权。
- 内部保留精度，仅在最终序列化舍入（比例 4 位、时长 3 位）。
- 空数据输出空分布或 null，不除零、不伪造。
- hostedCoverage 初版使用明确人物主持/出镜关键词证据；无法可靠判断时采用
  保守 0.0 并记录 CALIBRATION A-007。
- slot boundary aligned 以同步切边界占可判定边界比例 >= 0.5 为指标
  （CALIBRATION A-007）。
- cameraMovement 分布只用 camera-motion.json 的确定性值并保留原始枚举
  （不映射为摄影术语，docs/02 §4.8）。
- expectationChains 由 blockRelation 的跨 slot 引用确定性提取。

测试从完整 fixtures 重新计算预期值，不直接复用 fixture 中结果作为算法输入。

### 04-02：Profile 模型蒸馏

类型：feature/test  
依赖：04-01、03-03 文本模型端口
状态：已完成（2026-08-25，`analysis/profile_prompts.py` + `analysis/profile_pipeline.py` 编排，1041 tests 全绿）

模型只允许补充：

- L1 functionalTitle、narrativeFunction、intendedReaction。
- L2 carriage、pattern、referenceContent。
- hook/payoff 的主观表达。
- structureRequirements。
- adoptionHints。
- discussionItems。

必须实现：

- [x] 蒸馏请求只包含结构化 story/profile aggregate，不发送视频。
- [x] prompt 强调 affordance 和 L1/L2/L3 分层，不做具体地点/对象一一复刻。
- [x] 模型响应 schema 校验。
- [x] 确定性字段白名单保护；模型返回这些字段应被拒绝。
- [x] 模型失败时保留 aggregate，`distillStatus` 保持 skipped（未成功蒸馏的
      合法状态），report partial。
- [x] 模型成功且主观字段完整时 `distillStatus=complete`。

### 04-03：Profile CLI、输出与一致性

类型：feature/test  
依赖：04-01、04-02
状态：已完成（2026-08-25，1041 tests 全绿）

新增：

- `src/memoloupe/analysis/profile_pipeline.py`
- `src/memoloupe/cli/profile_build.py`

必须实现：

- [x] `memoloupe profile --output-dir DIR`。
- [x] 根目录原子写入 `style-profile.json`。
- [x] schemaVersion 固定为 2。
- [x] source revision、路径和时长来自 media/输出目录。
- [x] story slot 与 profile slot 集合一致。
- [x] block、shot、hook/payoff/turn 引用闭合。
- [x] durationShare、shotCount、avgShotSeconds、分布总和可复算。
- [x] 无模型和 Mock 模型两条 E2E。
- [x] strict validate 覆盖 profile。

实现要点（决策记录见 docs/06 D-036/D-037）：

- 输入门禁：media/shots/story-blocks 必需（退出码 3）；
- 聚合指纹含全部上游产物内容哈希；蒸馏指纹叠加 prompt 版本与服务标记；
  每次成功蒸馏后 checkpoint（`checkpoints/style-profile-distill.json`），
  重跑指纹命中不重发请求；
- 模型失败保留确定性聚合（distillStatus=skipped），report partial；
- story 重写会使 slot 集合失效的旧 style-profile 归档到
  `checkpoints/outdated/`，避免陈旧 Phase 3 产物污染 strict 校验。

## 6. Phase 05：真实服务、词表与校准

### Phase 目标

在不改变稳定契约的前提下，将 Mock-first 参考实现验证为可用于真实视频和真实服务的版本，并收敛待校准参数。

### 05-01：真实服务联调

类型：integration/config/test  
依赖：Phase 03、04 可用 Mock 完成后  
状态：05-01A~D 已完成（2026-08-26，决策记录 docs/06 D-041/D-042/D-044）

现有基础：UnifiedMLLM 和 ASR 已有 OpenAI-compatible 适配器。此计划不是从零编写，而是验证供应商兼容性并补齐缺口。

必须实现：

- [ ] 用目标 UnifiedMLLM 验证 video Data URI、JSON response format、batch 限制和超时。
- [x] 真正按 fallbackModel 切换模型重发，而不只记录 fallbackAttempted。
- [x] 验证 ASR 服务采用 JSON+base64 还是 multipart；必要时新增供应商适配器。
- [x] 为 story/profile 接入真实文本模型（`textModel` 配置 + CLI 注入；
  未配置时显式 warning 并降级）。
- [x] 真实服务测试默认 opt-in，不进入无凭据 CI。
- [x] 录制脱敏响应 fixture，覆盖供应商常见变体。
- [x] 日志确认不泄露 key、Data URI 和完整模型返回。
- [x] 提供 `.env.example` 配置示例（含 `--env-file` 加载，05-05）。

需要外部输入：端点、模型名、鉴权方式、供应商请求限制（真实 smoke
测试在 `MEMOLOUPE_RUN_REAL_SERVICE_TESTS=1` 且凭据齐全时启用）。

### 05-02：完整受控词表

类型：data/test  
依赖：可与 05-01 并行
状态：已完成（2026-08-26，1082 tests 全绿；决策记录见 docs/06 D-040）

- [x] 扩展 `rules/vocabulary.json` 到完整闭集。
- [x] 为别名、箭头变化、多选、unknown/unmapped 增加契约测试。
- [x] 词表版本进入模型 group 和 story/profile prompt 指纹。
- [x] 词表升级使相关缓存正确失效。
- [x] 对历史 unmapped 提供显式重算或迁移路径。

实现要点：

- v2 闭集 = docs/07 当前 modelShot 受控字段（18）+ story 8，契约测试锁定；
- 词表升 v2：补充 121 个英文/口语别名；`tests/contract/test_vocabulary_contract.py`
  锁定闭集清单、story_prompts 一致性、别名合法性（目标存在/单跳/casefold 唯一）、
  版本递增；
- `vocabVersion` 进入 story model_fill 与 profile aggregate/distill 指纹
  （unified group 指纹已有），词表升级自动失效全部相关缓存；
- unmapped 不持久化：词表升级后重跑阶段/重渲染即完成迁移。

### 05-03：黄金视频与参数校准

类型：test/calibration  
依赖：真实样例  
状态：框架已完成（2026-08-26：标注格式 + `core/calibration.py` 指标 +
opt-in 校准测试）；真实视频/标注到达后逐项校准。

校准项：

- A-001 视觉切镜。
- A-002 音频切点。
- A-003 BGM。
- A-004 质量。
- A-005 Apple Vision。
- A-006 story gap、无 ASR 和极短 block。
- A-007 profile hosted/boundary/distribution。

每项校准必须：

- [x] 保存脱敏黄金标注或期望范围（`tests/fixtures/golden/`，格式见 README）。
- [x] 先新增失败测试（`test_golden_calibration.py`，无标注时 skip）。
- [ ] 更新配置默认值和算法版本。
- [ ] 更新 fingerprint，确保旧缓存失效。
- [ ] 在 docs/06 记录实证、局限和适用范围。

### 05-04：HTML 体验、性能与发布准备

类型：feature/performance/docs  
依赖：Phase 03/04  
状态：05-05 的 CLI 能力部分已完成（2026-08-26，docs/06 D-043）；
shot HTML 品牌/交互生产模板已完成（2026-09-01，docs/06 D-050）；
story HTML、性能和发布文档仍等待视觉原型/真实长视频等外部输入。

- [x] 明确 shot HTML 视觉品牌和交互原型（shadcn-inspired 离线审片工作台）。
- [ ] 明确 story HTML 视觉品牌和交互原型。
- [x] 人工 correction 导出或 localhost review server（M3 已交付）。
- [x] confirmed/outdated/completion 状态闭环（M3 已交付）。
- [ ] 真实长视频性能基线。
- [ ] 支持的最大时长、格式和平台范围。
- [ ] 分享模式路径脱敏。
- [ ] 安装、分发、Apple Vision helper 编译说明。

## 7. 跨 Phase 约束

- Phase 2/3 的文本模型一律不得接收视频或帧。
- 模型“无”只能形成 `absent-claimed`。
- raw 检测证据不可被模型或人工覆盖。
- correction 使用 overlay，不直接修改 raw。
- 所有 ID、时间、引用和分布在写入后做跨文件校验。
- partial、scaffold、unavailable、skipped、failed 必须显式可见。
- 任何新模型字段先改 schema、fixtures 和契约测试。
- 任何最终边界变化必须失效所有区间相关下游。

## 8. 暂缺材料与处理方式

### 阻塞“功能等价验收”，不阻塞参考实现

- `comparison-memoclip-lapian-vs-MemoLoupe.md` 差距分析原文。
- memoclip-lapian 的 Phase 2/3 脱敏样例：story-blocks、story HTML、style profile。

没有这些材料时可以按当前契约完成 M3/M4，但只能声明“符合 MemoLoupe 规格”，不能独立证明“与 memoclip-lapian 功能等价”。

### 只阻塞 M5

- 真实 ASR、UnifiedMLLM、文本模型端点和鉴权。
- 完整词表。
- 真实黄金视频与标注。
- HTML 视觉/交互原型。
- 性能和最大视频时长目标。

## 9. GSD 导入建议

初始化 GSD 后，建议建立三个 phase：

```text
03-render-story-analysis
  03-01 review reason and aligned-cache closeout
  03-02 deterministic story scaffold
  03-03 story text-model orchestration
  03-04 story HTML, CLI, validation, E2E

04-style-profile
  04-01 deterministic profile aggregate
  04-02 profile model distillation
  04-03 profile CLI, output, validation, E2E

05-real-services-calibration
  05-01 real service integration
  05-02 vocabulary completion
  05-03 golden-video calibration
  05-04 HTML UX, performance, release readiness
```

依赖：03 → 04；05-01/05-02 可在 03/04 期间准备，但 05 的完成验收依赖 03 和 04。

## 10. 下一步

Phase 03、04 与 05-01A~D、05-02、05-03 框架、05-04（CLI 调试）、05-05
均已完成。剩余工作全部依赖外部输入：

- **05-01 真实 smoke**：提供 UnifiedMLLM/ASR/文本模型端点与凭据后，置
  `MEMOLOUPE_RUN_REAL_SERVICE_TESTS=1` 跑真实服务验证（opt-in 框架已就绪）；
  ASR 另有本地路径：`asr.provider=local-fireredvad-mlx`（D-045，
  `uv sync --extra asr-local`），无需远程端点；
- **05-03 参数校准**：提供 2~5 支真实视频 + 人工标注
  （`tests/fixtures/golden/` 格式），逐项回调 A-001~A-007；
- **05-06 HTML/性能/发布**：shot HTML 已按 D-050 落地；story 视觉原型与
  真实长视频到达后继续推进剩余品牌/交互、性能基线与发布文档。
