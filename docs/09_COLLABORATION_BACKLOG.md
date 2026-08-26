# MemoLoupe 协作开发 Backlog（Phase 05+）

状态：协作执行清单  
更新日期：2026-08-26  
基线：Phase 05-01A~D + 05-02 + 05-03 框架 + 05-04 + 05-05 已交付，`1125 passed, 6 skipped`  
依据：`docs/08_DEVELOPMENT_ROADMAP.md`、`MemoLoupe-todolist.md`、用户补充的
`MemoLoupe 未实现功能清单（复刻开发用）`

---

## 1. 使用方式

本文档用于多人或多 AI 协作开发。它不替代正式规格：

1. 稳定字段、状态、ID、时间和跨文件约束，以 `docs/07_SOURCE_DATA_CONTRACT.md`
   与 `docs/02_DATA_AND_STATE_CONTRACTS.md` 为准。
2. Phase 顺序、降级行为和系统边界，以 `docs/08_DEVELOPMENT_ROADMAP.md`
   为准。
3. 本文只负责把剩余 gap 拆成可领取、可验收的工作包。

每个任务的完成定义：

- schema / 配置 / 数据类先行，必要时先补失败测试；
- 最小实现后运行相关单元、契约、集成测试；
- 涉及产物变更时运行 `uv run memoloupe validate <output-dir> --strict`；
- 涉及新决策或校准时更新 `docs/06_DECISIONS_AND_ASSUMPTIONS.md`；
- 不得为了通过测试弱化更高优先级契约。

---

## 2. 当前状态速览

| 区域 | 状态 | 说明 |
|---|---|---|
| M0+M1 / M2 / M3 | 已完成 | 契约、确定性 Phase 1、音频/视觉/模型编排、人工校对闭环 |
| Phase 03 | 已完成 | story scaffold、文本模型编排、story HTML/CLI/校验 |
| Phase 04 | 已完成 | style-profile schema v2、确定性聚合、蒸馏、CLI/校验 |
| Phase 05-01A | 已完成 | story/profile 真实文本模型 CLI 注入、显式跳过、strict、`.env.example` |
| Phase 05-02 | 已完成 | 完整受控词表、版本化缓存失效、迁移护栏 |
| Phase 05 剩余 | 基本完成 | 05-01A~D/05-02/05-03 框架/05-04/05-05 已交付；剩余 = 真实服务 smoke、黄金校准回调、HTML 品牌/性能/发布（等外部输入） |

---

## 3. 推荐执行顺序

```text
05-01B UnifiedMLLM fallback 与恢复链路
  ↓
05-01C ASR 真实服务适配
  ↓
05-01D 真实服务 opt-in 测试与脱敏 fixture
  ↓
05-03 黄金视频校准
  ↓
05-04 Phase 1 CLI 生产调试能力
  ↓
05-05 配置可用性
  ↓
05-06 HTML UX、性能与发布准备
```

可并行项：

- 05-04、05-05 可与 05-01B/05-01C 并行；
- 05-06 的 HTML 品牌设计可先做原型，但性能基线依赖真实长视频；
- 05-03 依赖黄金视频与标注，未拿到材料前只做测试框架和参数入口。

---

## 4. Phase 05-01B：UnifiedMLLM fallback 与恢复链路

优先级：P0  
类型：integration / service / test  
依赖：现有 `services/unified_media.py`、`analysis/model_orchestrator.py`、
`docs/06 D-023`

### 目标

让 `fallbackModel` 成为真实行为：主模型失败时按 fallback 模型名重发请求，而不是
只记录 `fallbackAttempted`。同时核对 400 类确定性输入错误的恢复链路。

### 工作包

- [x] 05-01B-1：服务层支持 per-request model override
  - 在 UnifiedMLLM 请求对象或调用参数中允许覆盖模型名；
  - OpenAI-compatible 请求体实际写入 fallback 模型；
  - 日志和 artifact 只记录脱敏后的模型尝试信息。
- [x] 05-01B-2：编排器实现 fallback 重发
  - 首发失败后使用 `fallbackModel` 重发；
  - fallback 成功时产物记录主模型失败与 fallback 成功证据；
  - fallback 失败时保持现有 partial / failed 降级语义。
- [x] 05-01B-3：400 类输入恢复链路核对（单镜头重试与短镜头补齐核对；降分辨率/备用服务为显式配置留口，不静默启用）
  - 单镜头重试；
  - 短镜头补齐到统一最小时长；
  - 降分辨率重试；
  - 可选备用服务只作为显式配置，不静默启用。
- [x] 05-01B-4：测试
  - mock 服务断言两次请求的 model 不同；
  - fallback 成功、fallback 失败、无 fallbackModel 三类路径；
  - 不泄露 key、Data URI、完整模型返回。

### 预期文件

- `src/memoloupe/services/unified_media.py`
- `src/memoloupe/analysis/model_orchestrator.py`
- `tests/unit/test_unified_media_service.py`
- `tests/unit/test_model_orchestrator.py`
- `docs/06_DECISIONS_AND_ASSUMPTIONS.md`

### 验收

- fallback 成功时对应 shot/group 不再被误判为永久失败；
- 请求体中的 `model` 确实从主模型切换为 fallback 模型；
- 全量测试通过；
- 样例产物 strict validate 通过。

---

## 5. Phase 05-01C：ASR 真实服务适配

优先级：P0  
类型：integration / config / service  
依赖：真实 ASR 供应商协议

### 目标

确认并支持目标 ASR 服务的协议形态。当前实现偏 OpenAI 风格
`/audio/transcriptions`，原版 memoclip-lapian 使用 multipart 文件上传。

### 工作包

- [x] 05-01C-1：协议确认（JSON+base64 与 multipart 两种形态均已实现，multipart 为原版形态）
  - JSON + base64；
  - multipart file field；
  - 鉴权头、前缀、超时、TLS、供应商扩展字段。
- [x] 05-01C-2：适配器拆分（OpenAICompatibleASR / MultipartOpenAICompatibleASR）
  - 保留当前 OpenAI-compatible ASR；
  - 新增 multipart ASR adapter；
  - 通过配置选择 provider / transport。
- [x] 05-01C-3：配置映射（asr.provider/fileField/timeoutSec）
  - `asr.provider`；
  - `asr.baseUrl`、`asr.apiKey`、`asr.model`；
  - `asr.fileField`、`asr.timeoutSec`、`asr.insecureTls` 如确有需要。
- [x] 05-01C-4：降级和审计（未配置 skipped、失败 failed、multipart 日志脱敏）
  - 服务未配置时继续输出 `asr.json status=skipped`；
  - 服务失败时输出显式 failed / partial；
  - 日志不得泄露音频内容、key、完整供应商返回。

### 预期文件

- `src/memoloupe/services/asr.py`
- `src/memoloupe/core/config.py`
- `.env.example`
- `tests/unit/test_asr_service.py`
- `tests/unit/test_config.py`
- `docs/06_DECISIONS_AND_ASSUMPTIONS.md`

### 验收

- 无凭据 CI 下所有测试继续可运行；
- opt-in 凭据存在时可跑真实 ASR smoke；
- ASR 失败不会阻断后续 story scaffold；
- 产物满足 schema 与 cross-artifact 校验。

---

## 6. Phase 05-01D：真实服务 opt-in E2E 与脱敏 fixture

优先级：P0  
类型：test / fixture / observability  
依赖：05-01B、05-01C 的服务入口

### 目标

建立真实服务联调的可重复验收方式：默认不进入无凭据 CI，但开发者可在本地
通过环境变量显式启用。

### 工作包

- [x] 05-01D-1：定义 opt-in 开关（MEMOLOUPE_RUN_REAL_SERVICE_TESTS=1，缺凭据 skip）
  - 例如 `MEMOLOUPE_RUN_REAL_SERVICE_TESTS=1`；
  - 缺少凭据时 pytest skip，而不是 fail。
- [x] 05-01D-2：真实服务 smoke tests（UnifiedMLLM/ASR/文本模型最小调用）
  - UnifiedMLLM 最小 1-2 shot；
  - ASR 最小音频片段；
  - story/profile 文本模型最小 prompt。
- [x] 05-01D-3：脱敏响应 fixture（tests/fixtures/services/）
  - 保存供应商常见响应变体；
  - 删除 key、URL 签名、Data URI、完整敏感原文；
  - fixture 用于无网络回归测试。
- [x] 05-01D-4：日志审计测试（异常脱敏、checkpoint 无媒体载荷、配置快照脱敏）
  - 异常信息脱敏；
  - JSON report 不包含密钥；
  - checkpoint 不包含不可公开的媒体载荷。

### 预期文件

- `tests/integration/test_real_services_opt_in.py`
- `tests/fixtures/services/`
- `src/memoloupe/services/base.py`
- `docs/06_DECISIONS_AND_ASSUMPTIONS.md`

### 验收

- 无凭据环境：测试 skip 且不影响全量测试；
- 有凭据环境：shot → story → profile 的真实服务 smoke 能跑通；
- 脱敏 fixture 能覆盖至少一种真实返回变体。

---

## 7. Phase 05-03：黄金视频与参数校准

优先级：P1  
类型：calibration / test  
依赖：真实视频、人工标注或可接受期望范围

### 目标

用黄金样例收敛 `docs/06` 中 A-001 至 A-007 的待校准参数。没有黄金视频前，
不得凭感觉改默认阈值。

### 工作包

- [x] 05-03-1：黄金样例格式（tests/fixtures/golden/ + core/calibration.py 指标）
  - 定义标注 JSON：镜头边界、音频切点、BGM 区间、质量问题、story block；
  - 明确误差容忍：边界毫秒、召回/精确率、枚举匹配规则。
- [ ] 05-03-2：A-001 视觉切镜
  - 调整直方图、边缘、MAD-K 组合参数；
  - 记录误检/漏检案例。
- [ ] 05-03-3：A-002 音频切点
  - 校准六特征权重和阈值；
  - 与视觉切镜合并窗口一起评估。
- [ ] 05-03-4：A-003 BGM
  - 校准 musicLevelDb、bassEnergy、silentLevelDb；
  - 验证旁白、环境声、静音误判。
- [ ] 05-03-5：A-004 质量检测
  - blur、欠曝、过曝阈值；
  - 不同分辨率和编码下的稳定性。
- [ ] 05-03-6：A-005 Apple Vision 运镜
  - 光流/单应性参数；
  - Apple Vision 不可用时的 unavailable 语义保持。
- [ ] 05-03-7：A-006 Story 聚块
  - gapMs 1200 是否适合不同内容；
  - 无 ASR、极短 block、密集对白样例。
- [ ] 05-03-8：A-007 Profile 统计
  - hosted coverage；
  - audioBoundaryBySlot；
  - musicAlignment；
  - 分布按镜头数还是时长加权的最终判断。

### 预期文件

- `tests/fixtures/golden/`
- `tests/e2e/test_golden_calibration.py`
- `src/memoloupe/core/calibration.py`
- `docs/06_DECISIONS_AND_ASSUMPTIONS.md`

### 验收

- 每个 A-00x 都有实证记录；
- 参数变更进入 fingerprint，旧缓存失效；
- 黄金样例 strict validate 通过；
- 未达标项必须留有误差解释，不假装 complete。

---

## 8. Phase 05-04：Phase 1 CLI 生产调试能力

优先级：P1  
类型：cli / ux / test  
依赖：现有 `memoloupe shot`

### 目标

补齐与原版 memoclip-lapian 相比对人工联调影响最大的 CLI 能力。注意：
MemoLoupe 不必逐项照搬原版命令名，但必须保持跳过 ≠ absent 的契约。

### 工作包

- [x] 05-04-1：统一跳过开关（--skip STEP，跳过写降级产物，跳过 ≠ absent）
  - 设计 `--skip STEP` 可重复，或一组显式 `--skip-*`；
  - 覆盖 ASR、Unified、frames、camera-motion、quality、music、energy；
  - 跳过时产物状态为 skipped / unknown，不伪造 absent。
- [x] 05-04-2：dry-run（跳过全部可选步骤，与 --mock-services 区分）
  - 不调用任何外部服务；
  - 可验证切镜、clip、基础产物；
  - 与 `--mock-services` 区分清楚。
- [x] 05-04-3：render-only（只读 raw 重渲 HTML，不改 raw）
  - 只读取已有 raw 产物重渲 HTML；
  - 不触发检测和模型请求；
  - 模板/样式调整时可快速回归。
- [x] 05-04-4：strict（partial 返回非零，JSON report 供 CI）
  - shot pipeline 任一步 partial / failed 时返回非零；
  - 保留 JSON report 供 CI 读取。
- [x] 05-04-5：调试规模控制（--max-shots N、story --max-blocks N）
  - `--max-shots N`；
  - 可选 `--template PATH`；
  - story 的 `--max-blocks N` 可并入此工作包。

### 预期文件

- `src/memoloupe/cli/shot_analysis.py`
- `src/memoloupe/analysis/shot_pipeline.py`
- `src/memoloupe/render/shot_html.py`
- `tests/integration/test_shot_cli.py`
- `tests/e2e/test_phase1_e2e.py`

### 验收

- 每个 skip step 都有测试证明产物状态正确；
- dry-run 不产生外部服务请求；
- render-only 不改 raw JSON；
- strict 可用于 CI 失败门禁。

---

## 9. Phase 05-05：配置可用性

优先级：P2  
类型：config / cli / docs  
依赖：现有 `core/config.py`

### 目标

降低真实服务联调时的配置成本。当前已有 `.env.example`，但程序仍只读取进程
环境变量，不自动加载 `.env`。

### 工作包

- [x] 05-05-1：`.env` 加载策略（--env-file PATH，不覆盖已有变量，无文件时行为可预测）
  - 支持 `--env-file PATH`，或明确决定不支持并记录；
  - 不自动覆盖已经存在的环境变量；
  - 无文件时行为可预测。
- [x] 05-05-2：配置自检命令（memoloupe config，redacted_snapshot 输出 + 未配置服务标注）
  - 可选新增 `memoloupe config --print`；
  - 输出脱敏后的有效配置；
  - 标出未配置的真实服务项。
- [x] 05-05-3：环境变量别名（评估结论：不引入 snake_case 别名，避免双命名漂移，见 D-044）
  - 评估 camelCase 拼接变量是否增加 snake_case 别名；
  - 保持向后兼容。
- [x] 05-05-4：validate 可用性（结论：不实现 --require-status/--source-shot-document，等价检查 = validate --strict，见 D-044）
  - 核对原版 `--require-status` 与 `--source-shot-document` 是否需要；
  - 若不实现，记录 MemoLoupe 等价检查方式。

### 预期文件

- `src/memoloupe/core/config.py`
- `src/memoloupe/cli/main.py`
- `.env.example`
- `tests/unit/test_config.py`
- `docs/06_DECISIONS_AND_ASSUMPTIONS.md`

### 验收

- 新配置入口有单测；
- 输出不泄露密钥；
- README 或 `.env.example` 说明真实服务最小配置。

---

## 10. Phase 05-06：HTML UX、性能与发布准备

优先级：P2  
类型：frontend / performance / docs  
依赖：真实样例、视觉原型

### 目标

补齐人工使用体验和交付文档，让 MemoLoupe 能稳定用于真实长视频拉片。

### 工作包

- [ ] 05-06-1：HTML 视觉品牌
  - shot/story 统一设计令牌；
  - Light/Dark 主题；
  - 可读性、证据抽屉、状态标识一致性。
- [ ] 05-06-2：HTML 交互补强
  - URL 编码与本地文件播放兼容性；
  - story-analysis 视频定位；
  - corrections 操作防误触。
- [ ] 05-06-3：性能基线
  - 真实长视频耗时、内存、磁盘占用；
  - ffmpeg / clip / frame / model 并发上限；
  - 记录推荐最大视频时长。
- [ ] 05-06-4：媒体处理策略核对
  - source video 拷贝还是 symlink；
  - proxy 生成参数；
  - 短镜头 clip 补齐但不改原始检测边界。
- [ ] 05-06-5：发布文档
  - 安装说明；
  - Apple Vision helper 编译/不可用降级；
  - 真实服务配置示例；
  - 常见失败排查。

### 预期文件

- `templates/shot-analysis.html`
- `templates/story-analysis.html`
- `src/memoloupe/render/`
- `docs/README.md`
- `docs/06_DECISIONS_AND_ASSUMPTIONS.md`

### 验收

- 真实长视频样例可完成 shot → story → profile；
- HTML 离线打开、相对路径、无外链脚本；
- 性能数据写入文档；
- 发布文档可由新开发者按步骤跑通。

---

## 11. 设计分歧与明确不做项

以下内容来自原版 gap，但当前不应直接实现，除非先更新正式规格：

- Story 阶段 `--with-frames` / `--max-frames`：
  MemoLoupe 铁律是 Phase 2/3 文本模型不发送视频或帧。若要改，需要设计评审、
  契约更新和隐私/成本说明。
- 下游 Story Spine、用户素材匹配、自动粗剪、FCPXML 导出：
  不属于 MemoLoupe 核心输出边界。只能作为消费者验证，不进入核心流程。
- 模型声称“没有”直接写 `absent`：
  永远禁止；只能得到 `absent-claimed`。

---

## 12. 暂缺材料

这些材料会显著提升后续开发效率：

- 真实 UnifiedMLLM / ASR / 文本模型端点、模型名、鉴权方式、速率限制；
- 1-3 条可脱敏黄金视频与人工标注；
- 原版 memoclip-lapian 的脱敏 story/profile 样例产物；
- HTML 视觉品牌或交互原型；
- 目标运行环境范围：macOS 版本、Apple Vision 可用性、最大视频时长。

没有这些材料时，仍可先做：

- fallback 真正换模型重发；
- ASR multipart adapter 扩展点；
- opt-in 测试框架；
- CLI skip / dry-run / render-only / strict；
- `.env` 加载和配置自检。

---

## 13. GSD phase 建议

如果导入 GSD，可按以下 phase 建立：

```text
05-01B-unified-fallback-recovery
  - service model override
  - orchestrator fallback retry
  - input recovery parity
  - fallback and redaction tests

05-01C-asr-real-service
  - protocol decision
  - multipart adapter
  - config mapping
  - skip/failed semantics and tests

05-01D-real-service-opt-in-tests
  - opt-in env gate
  - smoke tests
  - sanitized fixtures
  - log redaction audit

05-03-golden-calibration
  - golden annotation format
  - A-001 through A-007 calibration
  - fingerprint invalidation
  - docs/06 evidence update

05-04-shot-cli-productivity
  - skip steps
  - dry-run
  - render-only
  - strict
  - max-shots / max-blocks

05-05-config-usability
  - env-file
  - config print
  - env aliases
  - validate option parity review

05-06-html-performance-release
  - visual brand
  - interaction polish
  - long-video performance
  - media strategy
  - release docs
```

