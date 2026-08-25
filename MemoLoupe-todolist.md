# MemoLoupe 后续开发任务清单

> 依据：`comparison-memoclip-lapian-vs-MemoLoupe.md` 的差距分析 + MemoLoupe `docs/` 规格与 `docs/06` 决策记录。
> 现状基线（2026-08-25 实测）：M0+M1 + M2 + M3 + Phase 03 已交付，**968 测试全过**（`uv run pytest -q`，48.28s）。
> 当前提交：`dcee671`（M3: 人工校对 + M3-M5 开发路线文档）。03-01~03-04 与 Phase 03 产物待提交。
> 目标：补全 Phase 3 / Phase 4，达到与 memoclip-lapian 功能等价。
>
> **重要**：MemoLoupe 已有官方执行路线 `docs/08_DEVELOPMENT_ROADMAP.md`（Phase 03/04/05，GSD plan 结构，含接口草案与验收标准）。
> 本清单是面向执行的任务视图，与 roadmap 的对应关系已标注到每一项；**冲突时以 docs/07 契约与 docs/08 路线为准**。
>
> 开发纪律（AGENTS.md）：schema/测试先行 → 失败测试 → 最小实现 → 单测+契约+集成 → 样例产物过 `memoloupe validate <dir> --strict` → 更新 docs/06。

---

## 里程碑总览

| 里程碑 | 内容 | 状态 |
|--------|------|------|
| M0+M1 | 可执行契约 + 确定性 Phase 1 | ✅ 已交付 |
| M2 | 音频与智能分析（六特征/BGM/Vision/模型编排） | ✅ 已交付 |
| M3 | 人工校对（corrections overlay + review server + 导入导出） | ✅ 已交付 |
| Phase 03 | 渲染收尾 + Phase 2 故事分析 | ✅ 已交付（03-01~03-04） |
| Phase 04 | Phase 3 风格档案（schema v2） | ⬜ 待开发 |
| Phase 05 | 真实服务、完整词表、真实样例校准与体验收尾 | ⬜ 待开发，可并行准备 |

---

## 零、设计分歧确认（已全部关闭 ✅）

> 本节任务已全部完成，无需再做；留档备查。

- [x] **T0.1 `bgm` 字段归属** ✅
  结论（docs/08 §2 已关闭）：BGM 存在性只由 `music-flags.json` 确定性检测负责；UnifiedMLLM 只输出 `bgmStyle`。MemoLoupe 方向确认为正式设计。
- [x] **T0.2 模型分组结构** ✅
  结论（docs/08 §2 已关闭）：visual/audio/editing_function 三组为内部执行，下游只消费合并后的稳定 `unified-media.json`。

---

## 一、Phase 1 收尾（roadmap 03-01，✅ 已完成）

- [x] **T1.1 needsReview 冲突理由接入渲染层** ✅（2026-08-25 收官，871 tests 全绿）
  已完成：`shot_html.py` 改用 `build_observations_with_review()`；合并 `shots.json.needsReview` 与 resolver 理由；模型内容 HTML escape；`title=` 属性展示；单测 `test_review_reasons_mark_column_header`。
  收尾三项（本轮完成，语义见 docs/04 §3.2/§8.2 与 docs/06 D-030）：
  - [x] `data-review-reasons` 机器可读属性：JSON 字符串数组，合并 resolver 理由与 shots.json 标记；不变量 needs-review="true" ⟺ reasons 非空。
  - [x] `html_contract.py` 校验属性存在性、JSON 合法性与一致性；strict 模式交叉核对 raw shots.json 的 needsReview 标记（单向）。
  - [x] resolver→render→validate 回归测试（`tests/integration/test_cli_html_validate.py::TestReviewReasonsRegression`）。
  验收达成：Vision 与模型运镜冲突时镜头列出现明确理由；无冲突时不显示空 badge；871 tests 全绿，样例产物过 `memoloupe validate --strict`。

- [x] **T1.2 aligned shots 指纹复用** ✅
  base/aligned 指纹复用已实现，`test_aligned_run_is_fully_reusable_on_second_run` 覆盖；第二次运行全部复用且不重复移动 final 边界。

---

## 二、Phase 2：故事分析（roadmap Phase 03，plans 03-02~03-04）

> 契约锚点已就绪：`schemas/story-blocks.json`（status: complete/scaffold；boundarySource: model/asr-gap）、`docs/03 §3`、`docs/02`。
> 铁律：聚块确定性（默认 asr-gap，模型为可选），叙事字段由文本模型填；**不发送视频或帧**（docs/06 已确认事实 17；roadmap 03-03 要求测试断言 payload 不含视频/Data URI）。

### 03-02 确定性 Story Scaffold
- [x] **T2.1 `analysis/story_pipeline.py` — 聚块与脚手架** ✅（2026-08-25，905 tests 全绿）
  - [x] 读取并校验 `media.json`/`shots.json`/`asr.json`/`unified-media.json`（asr/unified 缺失显式降级）。
  - [x] 构造镜头文本摘要 `build_shot_summaries()`：shotID、时间、visual.content、subjects/actions/setting、ASR speech、文字、转场、运镜分类；白名单复制，结构上**不含** clip/帧 Data URI/源视频路径/模型代理路径（测试断言）。
  - [x] ASR segments 按时间排序，按 `gapMs`（默认 1200，`>=` 切开）创建停顿段 `compute_speech_runs()`。
  - [x] `segment_of(start_ms, end_ms)`：最大重叠 run；零重叠归入最晚前置 run；片头静默并入首块；无 run 返回 sentinel -1。首镜头强制开块。
  - [x] block start/end 从首尾镜头 final 边界派生。
  - [x] 无 ASR（缺失/skipped/failed/空）时保守单块 scaffold。
  - [x] scaffold 填合法 unknown/default 叙事字段，`status=scaffold`、`boundarySource=asr-gap`、`slots=[]`。
  - [x] checkpoint 指纹含 shots、ASR、unified-media 内容哈希 + gapMs + 实现版本；同指纹重跑复用（generatedAt 不刷新）。
  - 附带契约修订：`informationRole` pattern 放宽允许 `unknown` 单值（向后兼容 widening，docs/06 D-031）。

### 03-03 Story 文本模型编排
- [x] **T2.2 文本模型适配** ✅（2026-08-25，923 tests 全绿）
  - [x] `TextModelService` 协议（`services/text_model.py`）：`TextModelRequest{task,prompt,system,max_tokens}` → 原始 JSON 文本；`MockTextModelService` 按调用序号编排成功/非法 JSON/漏 block/未知 block/暂时失败/永久失败。
  - [x] `OpenAICompatibleTextModel` 复用 `services/base.py` 的 HTTP/鉴权/脱敏（`/chat/completions`）。
  - [x] prompt 只含文本摘要（`analysis/story_prompts.py`，受控词表常量单一事实源）；测试断言 payload 无视频/Data URI/路径。
  - [x] 返回字段覆盖 block 全叙事字段；slot 返回 slotType/title/blockIDs/rationale。
  - [x] Schema 校验、受控词表归一化、ID 集合闭合（`parse_model_result`，不合规整体回退）。
  - [x] 模型不得新增/删除/重排 shot；不得改确定性 block 边界（不一致即判不合规；合并只复制叙事白名单）。
  - [x] 模型不可用或不合规时保留 scaffold（report partial），不丢候选 blocks；每次成功请求后 checkpoint（`checkpoints/story-blocks-model.json`），重跑指纹命中不重发请求。
  - 附带契约收紧：schema 增加 blockTitle≤12 / slotTitle≤15 maxLength（docs/06 D-032）。

### 03-04 Story HTML、CLI 与校验闭环
- [x] **T2.3 `render/story_html.py` + `templates/story-analysis.html`** ✅（2026-08-25，968 tests 全绿）
  - [x] 故事时间线、block、slot、镜头覆盖与关系视图；story-block DOM 只出现在 storyAnalysis 文档（块根 `class="story-block"` + data-story-block-id/data-shot-ids/data-start-ms/data-end-ms）。
  - [x] 复用 Phase 1 五态/confidence/evidenceRefs/verified 语义（叙事字段五态单元格，evidenceRefs 指向 `raw/story-blocks.json#blocks[N].field`）。
  - [x] 资源离线、路径相对、无外链脚本（D-011）；模型文本 HTML escape。
- [x] **T2.4 CLI `memoloupe story --output-dir DIR`** ✅
  - [x] 替换 `_cmd_not_implemented`（`cli/story_analysis.py` + 根级 `run_story_analysis.py` 薄包装）。
  - [x] 默认要求 shot analysis 可用（shots.json+media.json schema 合法，否则退出码 3）；`--allow-draft` 显式允许未确认输入；`--mock-text-model` 演示 mock（D-033）。
- [x] **T2.5 校验扩展** ✅
  - [x] story-blocks.json schema 校验 + story HTML 语义校验（html_contract 结构/严格一致性）。
  - [x] 跨文件校验 block→shot、slot→block、relation→block（cross_artifact `_check_story_blocks`，03-02 已随 scaffold 交付）。
  - [x] 生成最小样例（fixture minimal scaffold）与完整 Mock 样例（fixture output_full + mock fill），均过 `validate --strict`（`tests/e2e/test_phase2_e2e.py`）。

**Phase 03 成功标准**（✅ 全部达成）：默认 asr-gap 确定性聚块；模型失败仍出 scaffold；`memoloupe story` 可运行/可复用 checkpoint/可显式降级；`raw/story-blocks.json` 过 schema+跨文件严格校验；`story-analysis.html` 离线可开、具备五态/证据/校对语义；全量 968 测试通过并新增 Phase 2 E2E。

---

## 三、Phase 3：风格档案（roadmap Phase 04）

> 契约锚点已就绪：`schemas/style-profile.json`（schemaVersion 2，必填 structure/pacing/style/structureRequirements/adoptionHints/discussionItems/asrTextStats/distillStatus）、`docs/03 §4`。
> 铁律：**先确定性聚合，再模型蒸馏**；profile 模型不可用时确定性 profile 仍生成、distillStatus 非 complete。

### 04-01 确定性 Profile Aggregate
- [ ] **T3.1 `analysis/profile_aggregate.py` — 第一趟确定性聚合**（纯函数友好，**不调用模型**）
  - [ ] slot 序列、时间范围、durationShare、minBlocks。
  - [ ] L3 shotIds、shotCount、avgShotSeconds。
  - [ ] 全片镜头时长 mean/p50（可附 p10/p90）；densityCurve、slotPacing。
  - [ ] audioBoundaryBySlot、musicAlignment。
  - [ ] transition/framing/lighting/cameraMovement 分布。
  - [ ] textOverlay coverage、BGM coverage、speech/voiceMix coverage、hostedCoverage。
  - [ ] ASR segmentCount/characterCount/speechDurationMs；turns、nonLinearDevices、expectationChains。
  - 待校准（docs/06 A-007）：风格分布按镜头数还是时长加权（当前默认镜头数）、hosted coverage 规则、audio boundary aligned 的 slot 聚合阈值。

### 04-02 模型蒸馏与输出
- [ ] **T3.2 第二趟模型蒸馏**
  - [ ] 蒸馏 prompt 强调 docs/03 §4.2 约束。
  - [ ] 输出 structureRequirements/adoptionHints/discussionItems 等模型字段。
  - [ ] profile 模型失败时 distillStatus ≠ complete，确定性部分照常产出。
- [ ] **T3.3 `style-profile.json` 输出 + schema 校验**（写根目录，过 `schemas/style-profile.json` v2）。
- [ ] **T3.4 CLI `memoloupe profile`**（替换 `_cmd_not_implemented`，新建 `cli/profile_build.py`）。
- [ ] **T3.5 跨文件一致性**：style-profile 引用的 storyBlockID/shotID/slot 与上游 story-blocks、shots 对齐校验。

---

## 四、真实服务适配与校准（roadmap Phase 05，可与 03/04 并行准备）

- [ ] **T5.1 真实 UnifiedMLLM 适配**：真实端点/鉴权/最终 prompt（docs/06 §5 尚缺信息）；fallback 真正换模型重发需服务层支持（D-023 待办）。
- [ ] **T5.2 ASR / 文本模型真实服务**：端点、鉴权、供应商扩展字段。
- [ ] **T5.3 完整 `rules/vocabulary.json`**（当前小词表实现）。
- [ ] **T5.4 待校准参数标定**（docs/06 §4，用真实视频黄金样例）：A-001 视觉切镜、A-002 音频切点、A-003 BGM、A-004 质量、A-005 Apple Vision、A-006 故事聚块、A-007 Profile 统计。
- [ ] **T5.5 HTML 视觉品牌与交互原型**（docs/06 §5 尚缺）。

---

## 五、建议执行顺序

1. ~~T0.1 / T0.2~~（已关闭）。
2. **T1.1 剩余三项** —— 彻底关闭 03-01（`data-review-reasons` 机器语义 + html_contract 校验 + 回归测试），改动小、风险低，可先收官 Phase 1。
3. **T2.x（Phase 03）** —— 故事分析纵向切片：03-02 scaffold → 03-03 文本模型 → 03-04 HTML/CLI/校验。
4. **T3.x（Phase 04）** —— 风格档案：04-01 确定性聚合 → 04-02 蒸馏与输出。
5. **T5.x（Phase 05）** —— 真实服务与校准，可与 3/4 并行推进适配层。

每个里程碑完成后：跑全量 `uv run pytest -q`、生成样例产物过 `memoloupe validate <dir> --strict`、更新 `docs/06_DECISIONS_AND_ASSUMPTIONS.md`。

---

## 附：本次刷新（M3 提交 `dcee671`）清单外新增能力

人工校对闭环（初版清单未预见，原版 memoclip-lapian 没有）：
- `schemas/corrections.json` + `render/corrections.py`：纯追加 overlay，四态状态机（draft/underReview/confirmed/outdated，outdated 优先，confirmed 仅显式）。
- `analysis/completion.py`：completion 评估 + confirm 三道闸门。
- 校对 UI：词表下拉/verified 切换/pending 防丢/证据抽屉/区间播放/边界修正表单。
- `render/review_server.py` localhost API + `import-corrections` CLI。
- e2e：修正保留/重跑保留/revision 变更 outdated/confirm 全链路。
- 测试从 749 → 854（+105），全绿。
