# MemoLoupe 代码复现设计文档

这套文档把产品说明和稳定数据协议转换为可供开发型 AI 执行的工程规格。目标是复现一个行为等价、契约稳定、可测试、可校对、可追溯的 MemoLoupe，而不是猜测或逐行仿制不可获得的原始源码。

## 文档导航

| 文档 | 用途 |
|---|---|
| [00_REPRODUCTION_SPEC.md](00_REPRODUCTION_SPEC.md) | 产品范围、术语、系统不变量、成功标准 |
| [01_ARCHITECTURE_AND_MODULES.md](01_ARCHITECTURE_AND_MODULES.md) | 分层架构、目录、模块职责、依赖方向和核心接口 |
| [02_DATA_AND_STATE_CONTRACTS.md](02_DATA_AND_STATE_CONTRACTS.md) | 文件契约、ID、时间、状态、Observation、跨文件约束 |
| [03_PIPELINES_AND_ALGORITHMS.md](03_PIPELINES_AND_ALGORITHMS.md) | 三阶段流水线、算法、模型编排、缓存和失败恢复 |
| [04_UI_AND_VALIDATION.md](04_UI_AND_VALIDATION.md) | HTML 视图、人工校对、保存机制和语义校验器 |
| [05_TESTING_AND_ACCEPTANCE.md](05_TESTING_AND_ACCEPTANCE.md) | 测试分层、黄金样例、验收门槛和交付顺序 |
| [06_DECISIONS_AND_ASSUMPTIONS.md](06_DECISIONS_AND_ASSUMPTIONS.md) | 已选默认方案、已知冲突、待校准参数和变更规则 |
| [07_SOURCE_DATA_CONTRACT.md](07_SOURCE_DATA_CONTRACT.md) | 用户提供的完整数据协议原文，生成 schema 和模型时的字段级依据 |
| [08_DEVELOPMENT_ROADMAP.md](08_DEVELOPMENT_ROADMAP.md) | M3–M5 当前后续路线、GSD-ready phase/plan、验收和缺失材料 |
| [09_COLLABORATION_BACKLOG.md](09_COLLABORATION_BACKLOG.md) | Phase 05+ 剩余 gap 的协作开发 backlog、任务包、依赖和验收标准 |

## 规范词

- **MUST / 必须**：稳定契约或系统不变量，违反即不兼容。
- **MUST NOT / 禁止**：明确禁止的行为。
- **SHOULD / 应该**：推荐实现；偏离时必须记录理由并补测试。
- **MAY / 可以**：实现选择，不影响兼容性。
- **CALIBRATION / 待校准**：结构已确定，但参数或算法需要真实视频调优。

## 信息来源与可信度

设计文档融合两类输入：

1. MemoLoupe 架构与复现说明：定义三阶段、分层、确定性优先、五态语义、模型编排和 HTML 校对原则。
2. MemoLoupe 数据契约 1.0：定义文件结构、字段、ID、枚举、样例和下游消费方式。

当来源没有给出足够细节时，本设计选择可替换、可配置、可测试的默认实现，并在 `06_DECISIONS_AND_ASSUMPTIONS.md` 登记。

## 推荐阅读方式

- 产品或架构评审：阅读 00、01、06。
- 数据模型实现：阅读 00、02、05、07。
- 媒体分析实现：阅读 01、03、05。
- 模型服务实现：阅读 02、03、05。
- Web/HTML 实现：阅读 02、04、05。
- 端到端开发 AI：按编号顺序全部阅读。
- 后续阶段规划或执行：阅读 06、08、09，并回查相应专项规格。

## 文档自身的完成定义

这些文档描述的是首个兼容实现，而不是冻结所有算法。实现过程中允许调整待校准参数，但不得破坏：

- 文件级数据结构；
- 五态语义；
- 证据追溯；
- 镜头边界双轨记录；
- JSON 主契约；
- 三阶段产品边界；
- 失败可见和可恢复性。
