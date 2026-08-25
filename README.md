# MemoLoupe

面向"拉片"的分阶段媒体分析系统参考实现。从参考视频产出：

1. Phase 1：`shot-analysis.html`（镜头时间线与镜头级观察）
2. Phase 2：`story-analysis.html`（故事块与故事插槽）
3. Phase 3：`style-profile.json`（可复刻的叙事/节奏/风格契约）

产品边界止于 style profile；Story Spine、素材匹配、粗剪、FCPXML 均为外部消费者。

## 文档

开发前必须按序阅读 `docs/`（见 `AGENTS.md`）：

- `docs/00_REPRODUCTION_SPEC.md`：产品边界与系统不变量
- `docs/01_ARCHITECTURE_AND_MODULES.md`：分层架构与模块职责
- `docs/02_DATA_AND_STATE_CONTRACTS.md`：数据/状态契约实现约束
- `docs/07_SOURCE_DATA_CONTRACT.md`：字段级数据来源契约
- `docs/08_DEVELOPMENT_ROADMAP.md`：M3–M5 当前执行路线与 GSD-ready plan

## 开发

```bash
uv sync
uv run pytest
uv run memoloupe --help
```
