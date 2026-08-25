# MemoLoupe AI 开发指引

本仓库用于从零复现 MemoLoupe。任何开发型 AI 在修改代码前，必须依次阅读：

1. `docs/README.md`
2. `docs/00_REPRODUCTION_SPEC.md`
3. `docs/01_ARCHITECTURE_AND_MODULES.md`
4. `docs/02_DATA_AND_STATE_CONTRACTS.md`
5. `docs/03_PIPELINES_AND_ALGORITHMS.md`
6. `docs/04_UI_AND_VALIDATION.md`
7. `docs/05_TESTING_AND_ACCEPTANCE.md`
8. `docs/06_DECISIONS_AND_ASSUMPTIONS.md`
9. `docs/07_SOURCE_DATA_CONTRACT.md`（实现具体字段或生成 schema 时必须查阅）
10. `docs/08_DEVELOPMENT_ROADMAP.md`（M3–M5 当前执行路线）

## 规范优先级

发生冲突时按以下顺序处理：

1. `docs/07_SOURCE_DATA_CONTRACT.md` 的稳定字段定义，以及 `docs/02_DATA_AND_STATE_CONTRACTS.md` 中标注为 MUST 的实现约束。
2. `docs/00_REPRODUCTION_SPEC.md` 中的产品边界和系统不变量。
3. `docs/03_PIPELINES_AND_ALGORITHMS.md` 中的阶段顺序和降级行为。
4. `docs/01_ARCHITECTURE_AND_MODULES.md` 中的模块边界。
5. `docs/04_UI_AND_VALIDATION.md` 中的呈现和人工校对约束。
6. `docs/06_DECISIONS_AND_ASSUMPTIONS.md` 中的推荐默认值。
7. `docs/08_DEVELOPMENT_ROADMAP.md` 中的阶段拆分与任务状态；它不得覆盖以上产品和数据契约。

不得为了让测试通过而弱化更高优先级的契约。若实现与文档矛盾，应修改实现；若确需改变契约，先更新设计文档、契约版本和迁移策略。

## 强制工程规则

- 确定性检测优先于模型推断。
- 模型声称“没有”只能得到 `absent-claimed`，不得得到 `absent`。
- 人工核实状态 `verified` 与五态取值相互独立。
- 每个呈现给用户的分析值必须能追溯到一个或多个 raw 证据。
- 检测边界与最终边界必须同时保留；不得用人工或音频对齐值覆盖原始检测值。
- JSON 是机器主契约，HTML 是人工校对视图。
- 阶段必须可断点续跑；模型、ASR、Apple Vision 不可用时应产生显式状态并尽可能继续。
- 写入 JSON 必须采用临时文件加原子替换，避免生成半文件。
- 所有跨文件引用都必须在写入后校验。
- 所有时间使用整数毫秒；统计展示字段除外。
- 所有镜头时间区间采用 `[startMs, endMs)`。
- 不得静默吞掉未知字段、旧版本或非法枚举。

## 推荐开发节奏

每次只完成一个可验证的纵向切片：

1. 先定义或更新 schema、数据类和测试夹具。
2. 编写失败测试。
3. 实现最小功能。
4. 运行单元测试、契约测试和相关集成测试。
5. 生成一个小型样例产物并运行校验器。
6. 更新 `docs/06_DECISIONS_AND_ASSUMPTIONS.md` 中已解决或新增的假设。

第一阶段不得在数据模型尚未稳定时直接开发复杂前端或接入真实模型。

## 实现边界

MemoLoupe 的输出止于：

- `shot-analysis.html`
- `story-analysis.html`
- `style-profile.json`

本仓库不负责 Story Spine 生成、用户素材匹配、自动粗剪或 FCPXML 导出；这些只能作为下游消费者验证数据是否足够，不能进入核心流程。
