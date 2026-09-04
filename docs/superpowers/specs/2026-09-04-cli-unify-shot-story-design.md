# CLI 入口统一：删除 story 命令与 story-analysis.html 设计

日期：2026-09-04

## 背景与动机

当前 CLI 是三阶段入口：`memoloupe shot`（Phase 1+2 合并流程）/ `memoloupe story`（Phase 2 独立重跑）/ `memoloupe profile`（Phase 3）。其中 `story` 显得多余：

- `shot` 默认已链式完成镜头 + 故事两个阶段（`--skip-story` 才只跑 Phase 1），`story` 独立命令的唯一价值是 corrections 使故事失效后的重跑入口与 confirmed 门禁。
- 故事分析结果已合并呈现在 shot-analysis.html 的故事轨道（D-051）；独立的 story-analysis.html 与主工作台信息重复。

## 决策记录（用户已确认）

- 删除 `memoloupe story` 子命令；`shot --story-only` 承接其全部能力（含 confirmed 门禁）。
- 保留 `--skip-story`，行为不变。
- 一并删除 story-analysis.html 产物；故事结果只通过 shot-analysis.html 呈现。
- `profile` 命令保持原样；`analysis/story_pipeline.py` 管线逻辑零改动。

## 设计

### 1. CLI 层

- `src/memoloupe/cli/main.py`：移除 `story` 子命令注册与 `_dispatch` 中的 `story` 分支；模块 docstring 与 parser description 更新为「shot（镜头+故事）/ profile 两阶段」措辞。
- 根目录 `run_story_analysis.py` 薄包装删除。
- `src/memoloupe/cli/shot_analysis.py`：
  - 新增 `--story-only`：不跑镜头阶段，直接以原独立 story 命令的语义执行故事分析——默认要求 shotAnalysis corrections 为 `confirmed`（否则退出码 3），`--allow-draft` 显式跳过门禁。
  - 原 story 命令参数全部平移到 shot：`--allow-draft`、`--max-blocks`、`--mock-text-model`、`--scaffold-only`（`--gap-ms`/`--force`/`--no-cache`/`--strict`/`--json-report` shot 已有）。这些参数只在 `--story-only` 或默认链式流程中有意义。
  - 互斥校验：`--story-only` 与 `--skip-story`、`--render-only`、`--max-shots`、位置参数 `input` 冲突时报退出码 2。`--story-only` 时 `input` 位置参数改为可选（`nargs="?"`）。
  - `_run_chained_story` 由「组装 argv 调 `run_story_analysis`」保持现状或改为直接函数调用（实现时择一，行为等价）。
- `src/memoloupe/cli/story_analysis.py`：保留为实现模块供 shot 调用，删除其中 story-analysis.html 的渲染调用与相关输出（见下）；文件 docstring 更新。

### 2. 产物层：删除 story-analysis.html

- 删除 `src/memoloupe/render/story_html.py` 与 `templates/story-analysis.html`。
- story 流程（无论链式还是 `--story-only`）完成后只重渲 shot-analysis.html（现有 D-051 逻辑保留，渲染目标只剩它）。
- `memoloupe validate`：不再把 story-analysis.html 列为校验对象；若 target 目录中**存在** story-analysis.html（旧版残留），输出一条 warning（「旧版产物，可删除」），不产生 error，保证旧 output-dir 仍可校验通过。

### 3. 文档

- `AGENTS.md`：实现边界改为「输出止于 shot-analysis.html 与 style-profile.json」。
- `docs/01_ARCHITECTURE_AND_MODULES.md` §10：CLI 章节更新（删除 story 命令，shot 参数表补 `--story-only` 等）。
- `docs/06_DECISIONS_AND_ASSUMPTIONS.md`：D-051/D-056 相关措辞更新，并新增一条决策记录本次变更与理由。
- `docs/08_DEVELOPMENT_ROADMAP.md`：涉及独立 story 命令的任务状态措辞更新。
- `README.md` / `README.zh-CN.md`：用法示例更新。

### 4. 测试

- `tests/integration/test_story_cli.py`：改为通过 `shot --story-only` 驱动，覆盖 confirmed 门禁（退出码 3）、`--allow-draft`、`--scaffold-only` 与 `--mock-text-model` 互斥（退出码 2）。
- `tests/integration/test_shot_story_chain.py`、`tests/e2e/test_phase2_e2e.py`、`tests/e2e/test_phase3_e2e.py`、`tests/integration/test_cli_dispatch.py`、`tests/integration/test_connect_routing.py`：移除/改写对独立 story 命令与 story-analysis.html 的断言。
- 新增断言：story 阶段完成后 shot-analysis.html 含故事轨道且目录下无 story-analysis.html；validate 对残留 story-analysis.html 只给 warning。

## 不变量

- JSON 契约零改动：`raw/story-blocks.json` 等 raw 产物、schema、校验规则全部保留。
- 故事分析管线（scaffold + 文本模型填充）行为、退出码语义不变。
- 确定性渲染、断点续跑、降级显式状态等仓库工程规则不受影响。
- corrections → story 失效 → `shot --story-only` 重跑的工作流能力不丢。

## 验证

- 全量 `pytest tests/unit tests/contract tests/integration -q` 通过；e2e（mock 服务）通过。
- 对现有 output 样例跑 `memoloupe shot --story-only --mock-text-model` 与 `memoloupe validate`，确认产物与校验行为符合上述约定。
