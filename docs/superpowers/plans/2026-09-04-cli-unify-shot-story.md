# CLI 入口统一（删除 story 命令与 story-analysis.html）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除 `memoloupe story` 子命令（能力由 `memoloupe shot --story-only` 完整承接），并停止生成 story-analysis.html（故事结果只呈现在 shot-analysis.html）。

**Architecture:** CLI 层入口收敛：`cli/story_analysis.py` 保留为实现模块但不再注册为子命令；`cli/shot_analysis.py` 新增 `--story-only` 路径直接透传原 story 参数。产物层删除 story HTML 渲染链（renderer、模板、review server 路由）；validate 对残留 story-analysis.html 只 warning。契约毗邻点：`storyAnalysisPath` 与 completion 的 `_HTML_FILES` 映射值改为 `shot-analysis.html`，schema 版本不变。

**Tech Stack:** Python 3 / argparse / pytest（`.venv/bin/pytest`）。

**Spec:** `docs/superpowers/specs/2026-09-04-cli-unify-shot-story-design.md`（含 §2.1 契约毗邻点）

## Global Constraints

- 测试命令统一 `.venv/bin/pytest`；CLI 用 `.venv/bin/memoloupe`。
- JSON 契约零改动：raw 产物、schema、`rules/completion.json`、`schemas/corrections.json`（`documentType` enum 保留 `storyAnalysis`）全部不动。
- `storyAnalysisPath` 字段保留，值改 `"shot-analysis.html"`；`_HTML_FILES["storyAnalysis"]` 值改 `"shot-analysis.html"`。
- corrections → `shot --story-only` 重跑工作流能力不丢（含 confirmed 门禁，退出码 3；`--allow-draft` 绕过）。
- 故事分析管线 `analysis/story_pipeline.py` 零改动。
- validate 对残留 story-analysis.html 只 warning、不 error。
- `validate/html_contract.py` 的 storyAnalysis 文档类型规则**保留不动**（兼容旧残留页面；明确不在本次范围）。
- `docs/superpowers/` 历史 specs/plans、`CLI_CONNECT_TODO.md`、`MemoLoupe-todolist.md`、`comparison-*.md`、`docs/09` 一律不改（历史记录）。
- 故事轨道在 shot-analysis.html 的呈现与 corrections overlay 行为不回归。

---

### Task 1: shot 获得 `--story-only` 与原 story 全部参数；摘除 story 子命令

**Files:**
- Modify: `src/memoloupe/cli/shot_analysis.py`（`_build_parser` 56-131、`run_shot_analysis` 154-252）
- Modify: `src/memoloupe/cli/main.py`（docstring 1-21、`:43` import、`:56` description、`:87-92` 子命令循环、`:243-244` dispatch）
- Delete: `run_story_analysis.py`
- Test: `tests/integration/test_story_cli.py`（逐用例改驱动入口，断言不变）、`tests/integration/test_cli_dispatch.py`

**Interfaces:**
- Consumes: 现有 `run_story_analysis(argv: Sequence[str]) -> int`（`cli/story_analysis.py`，本任务不改其签名与行为）。
- Produces: `memoloupe shot --story-only` 路径——接受 `--output-dir`、`--gap-ms`、`--allow-draft`、`--force`、`--no-cache`、`--max-blocks`、`--mock-text-model`、`--scaffold-only`、`--strict`、`--json-report`，组装 argv 调 `run_story_analysis`。后续任务继续依赖 `run_story_analysis` 可导入。

- [ ] **Step 1: 改 `tests/integration/test_story_cli.py` 驱动入口（失败测试）**

全文把 `main(["story", "--output-dir", str(work), ...])` 形式的调用改为 `main(["shot", "--story-only", "--output-dir", str(work), ...])`（保持其余参数原样、顺序为 `--story-only` 打头）。`test_strict_returns_failed_on_text_model_failure`（:107-122）的 monkeypatch 目标 `story_cli.build_text_model_service` 不变。文件 docstring 与类名 `TestStoryCLI` 改为 `TestStoryOnlyCLI` 语义化命名（如 `class TestStoryOnlyCLI`）。

新增两个 shot 侧互斥用例：

```python
    def test_story_only_rejects_input_positional(self, tmp_path):
        work = _copy_fixture(tmp_path)
        code = main(["shot", "some.mp4", "--story-only", "--output-dir", str(work)])
        assert code == 2

    def test_story_only_conflicts_with_skip_story(self, tmp_path):
        work = _copy_fixture(tmp_path)
        code = main(["shot", "--story-only", "--skip-story", "--output-dir", str(work)])
        assert code == 2
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/integration/test_story_cli.py -x -q`
Expected: FAIL（argparse 不认识 `--story-only`，退出码 2 类错误或 SystemExit）

- [ ] **Step 3: 实现 shot 侧参数与分支**

`src/memoloupe/cli/shot_analysis.py` `_build_parser`：
- `input` 改为 `parser.add_argument("input", type=Path, nargs="?", help="源视频路径（--story-only 时省略）")`
- 追加：

```python
    parser.add_argument(
        "--story-only",
        action="store_true",
        help="只跑故事分析阶段（承接原 memoloupe story；默认要求 shotAnalysis 已 confirmed）",
    )
    parser.add_argument(
        "--allow-draft",
        action="store_true",
        help="--story-only：显式允许未确认/草稿输入（跳过 confirmed 门禁）",
    )
    parser.add_argument(
        "--max-blocks",
        type=int,
        default=None,
        metavar="N",
        help="--story-only 调试：只保留前 N 个 block",
    )
    parser.add_argument(
        "--mock-text-model",
        action="store_true",
        help="--story-only：文本模型使用可编程 mock（不发起网络请求）",
    )
    parser.add_argument(
        "--scaffold-only",
        action="store_true",
        help="--story-only：只生成确定性 scaffold，不调用文本模型",
    )
```

`run_shot_analysis` 在 `args = _build_parser().parse_args(...)` 之后、`_run_render_only` 分支之前插入：

```python
    if args.story_only:
        conflicts = []
        if args.input is not None:
            conflicts.append("位置参数 input")
        if args.skip_story:
            conflicts.append("--skip-story")
        if args.render_only:
            conflicts.append("--render-only")
        if args.max_shots is not None:
            conflicts.append("--max-shots")
        if args.start_ms is not None or args.end_ms is not None:
            conflicts.append("--start-ms/--end-ms")
        if args.mock_services:
            conflicts.append("--mock-services（请改用 --mock-text-model）")
        if args.dry_run:
            conflicts.append("--dry-run（请改用 --scaffold-only）")
        if args.skip:
            conflicts.append("--skip")
        if args.align_shot_boundaries_to_audio:
            conflicts.append("--align-shot-boundaries-to-audio")
        if conflicts:
            print(
                f"错误：--story-only 与 {'、'.join(conflicts)} 不能同时使用",
                file=sys.stderr,
            )
            return EXIT_USAGE
        story_argv = ["--output-dir", str(args.output_dir), "--gap-ms", str(args.gap_ms)]
        if args.allow_draft:
            story_argv.append("--allow-draft")
        for step in args.force:
            story_argv += ["--force", step]
        if args.no_cache:
            story_argv.append("--no-cache")
        if args.max_blocks is not None:
            story_argv += ["--max-blocks", str(args.max_blocks)]
        if args.mock_text_model:
            story_argv.append("--mock-text-model")
        if args.scaffold_only:
            story_argv.append("--scaffold-only")
        if args.strict:
            story_argv.append("--strict")
        if args.json_report:
            story_argv.append("--json-report")
        return run_story_analysis(story_argv)
    if args.input is None:
        print("错误：缺少源视频路径 input（或使用 --story-only）", file=sys.stderr)
        return EXIT_USAGE
```

文件 docstring 中补一句 `--story-only` 说明。

- [ ] **Step 4: 摘除 story 子命令**

`src/memoloupe/cli/main.py`：
- 删除 `from .story_analysis import run_story_analysis`（:43）。
- 删除 `_dispatch` 中 `if argv[:1] == ["story"]: return run_story_analysis(argv[1:])`（:243-244）。
- 子命令循环（:87-90）改为只剩 `("profile", "Phase 3：风格档案")`。
- 模块 docstring：删 `memoloupe story` 行，`memoloupe shot` 行改为「镜头 + 故事分析（--skip-story 只跑镜头；--story-only 只跑故事）」；parser description（:56）改为 `"MemoLoupe 拉片分析：shot（镜头+故事）/ profile 两阶段与产物校验。"`。
- 删除根级 `run_story_analysis.py`。

- [ ] **Step 5: 改 `tests/integration/test_cli_dispatch.py`**

parametrize 列表（:10-17）中删除 `("story", "--scaffold-only")`，新增 `("shot", "--scaffold-only")`（shot 帮助现在应含该 flag）。

- [ ] **Step 6: 运行相关测试**

Run: `.venv/bin/pytest tests/integration/test_story_cli.py tests/integration/test_cli_dispatch.py tests/integration/test_shot_story_chain.py tests/integration/test_connect_routing.py -q`
Expected: 全部通过

- [ ] **Step 7: Commit**

```bash
git add src/memoloupe/cli/shot_analysis.py src/memoloupe/cli/main.py tests/integration/test_story_cli.py tests/integration/test_cli_dispatch.py
git rm run_story_analysis.py
git commit -m "feat: shot --story-only 承接 story 命令能力，摘除 story 子命令"
```

---

### Task 2: 删除 story-analysis.html 产物链

**Files:**
- Modify: `src/memoloupe/cli/story_analysis.py`（docstring :5、import :49、渲染块 :258-286）
- Modify: `src/memoloupe/render/review_server.py`（docstring :1-17、import :37、`_ALLOWED_TOP` :43-49、`_rerender_story` :123-131、`_rerender_document` :133-137、GET 路由 :188-197）
- Modify: `src/memoloupe/cli/main.py`（`_cmd_validate` :107-111）
- Modify: `src/memoloupe/analysis/completion.py:43`、`src/memoloupe/analysis/profile_aggregate.py:784`
- Delete: `src/memoloupe/render/story_html.py`、`templates/story-analysis.html`、`tests/unit/test_story_html.py`
- Test: `tests/integration/test_story_cli.py`、`tests/integration/test_review_server.py`、`tests/unit/test_profile_aggregate.py:330`、`tests/unit/test_artifacts.py:78`、`tests/fixtures/output_full/style-profile.json:85`、`tests/fixtures/minimal/style-profile.json:26`、`tests/e2e/test_phase2_e2e.py`、`tests/e2e/test_phase3_e2e.py:172`

**Interfaces:**
- Consumes: Task 1 的 `shot --story-only`。
- Produces: story 阶段完成后目录下**无** story-analysis.html；`run_story_analysis` 的 JSON 报告不再有 `storyHtml` 键；validate 对残留 story-analysis.html 输出 severity=warning 的 issue（artifact `"story-analysis.html"`，`ValidationIssue` 来自 `memoloupe.validate.json_contracts`，字段 severity/artifact/json_path/message/expected/actual）。

- [ ] **Step 1: 改测试（失败先行）**

`tests/integration/test_story_cli.py`：
- `test_story_only_renders_workbench`（原 renders_html 用例）：删除 story-analysis.html 存在与 `data-document-type="storyAnalysis"` 断言，改为：

```python
        assert (work / "raw" / "story-blocks.json").is_file()
        assert not (work / "story-analysis.html").exists()
        shot_html = (work / "shot-analysis.html").read_text(encoding="utf-8")
        assert 'id="story-timeline-band"' in shot_html
```

- `test_validate_checks_story_html`（:162-174）整体改写为残留文件 warning 用例：

```python
    def test_validate_warns_on_legacy_story_html(self, tmp_path, capsys):
        work = _copy_fixture(tmp_path)
        assert main(["shot", "--story-only", "--output-dir", str(work), "--allow-draft"]) == 0
        assert not (work / "story-analysis.html").exists()
        # 模拟旧版残留：validate 只 warning，不 error。
        (work / "story-analysis.html").write_text("<html></html>", encoding="utf-8")
        code = main(["validate", str(work)])
        assert code == 0
        out = capsys.readouterr().out
        assert "story-analysis.html" in out
        assert "warning" in out
```

（若该文件已有 `_copy_fixture`/导入模式不同，沿用它；capsys 读取 stdout。）
- `test_json_report_includes_story_html`（:186-193）改名 `test_json_report_story_phase`：删 `storyHtml` 断言，保留 `report["phase"] == "story"` 与 exit 0。

`tests/integration/test_review_server.py`：
- 删除 :26 import 与 fixture :63 `render_story_html(out_dir, server_mode=True)` 行。
- 删除 `TestGet.test_story_analysis_html_by_name`（:98-102）。
- `TestPostCorrections.test_story_corrections_persist_to_story_document`（:172-200）：保留 POST 与 corrections 落盘断言（:172-195）；:197-200 的 GET `/story-analysis.html` 改为：

```python
        status, html_text = _request(base, "/shot-analysis.html")
        assert status == 200
        assert "新标题" in html_text  # 用该用例实际 POST 的替换标题文本
```

`tests/unit/test_profile_aggregate.py:330`：`assert source["storyAnalysisPath"] == "shot-analysis.html"`。
`tests/unit/test_artifacts.py:78`：夹具值改 `"shot-analysis.html"`。
`tests/fixtures/output_full/style-profile.json:85` 与 `tests/fixtures/minimal/style-profile.json:26`：`storyAnalysisPath` 值的文件名部分改为 `shot-analysis.html`（保持原路径前缀不动）。

`tests/e2e/test_phase2_e2e.py`：
- 删除 :27 `from memoloupe.render.story_html import render_story_html`；docstring :3-10 链路描述删 story-analysis.html。
- `_strict_errors`（:37-42）删 story_html 分支（只留 `validate_output_dir` + shot html 校验——若原 helper 校验了 shot html 则保留那部分，只删 story 部分）。
- 删 :116-119、:144-145、:171 的 `render_story_html(...)` 调用与 story-analysis.html 断言；各用例补 `assert not (work / "story-analysis.html").exists()`。
- `TestPhase2CliChain`（:189-197）：`main(["story", ...])` 改为 `main(["shot", "--story-only", "--output-dir", str(work), "--mock-text-model", "--allow-draft"])`。

`tests/e2e/test_phase3_e2e.py:170-172`：`main(["story", ...])` 改为 `main(["shot", "--story-only", "--output-dir", str(work), "--mock-text-model", "--allow-draft"])`，注释同步。

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/pytest tests/integration/test_story_cli.py tests/integration/test_review_server.py tests/unit/test_profile_aggregate.py tests/unit/test_artifacts.py -q`
Expected: FAIL（story-analysis.html 仍在生成 / storyAnalysisPath 旧值）

- [ ] **Step 3: 删除渲染链并改实现**

`src/memoloupe/cli/story_analysis.py`：
- 删 :49 `from memoloupe.render.story_html import render_story_html`。
- :258-286 渲染块替换为（删 story 渲染、`render_failed` 与 `storyHtml`，保留 D-051 工作台重渲）：

```python
    # D-051：story 结果合并进 shot 工作台。story 完成后必须重渲
    # shot-analysis.html，否则工作台的故事轨道停留在 story 之前的旧状态。
    # 工作台重渲失败只记 warning，不影响 story 产物与退出码。
    if report.status != "failed":
        try:
            render_shot_html(out_dir)
        except Exception as exc:
            print(
                f"  [warning] 重渲 shot-analysis.html（合并故事轨道）失败：{exc}",
                file=sys.stderr,
            )

    if args.json_report:
        json.dump(report.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        _print_summary(report)

    if report.status == "failed":
        return EXIT_STAGE_FAILED
    if args.strict and report.status == "partial":
        return EXIT_STAGE_FAILED
    return EXIT_OK
```

- 模块 docstring :3-5 删「渲染 story-analysis.html」表述，改为「完成后重渲 shot-analysis.html（故事轨道合并呈现）」。

`src/memoloupe/render/review_server.py`：
- 删 :37 import。
- `_ALLOWED_TOP`（:43-49）删 `"story-analysis.html",`。
- 删 `_rerender_story`（:123-131）；`_rerender_document`（:133-137）的 storyAnalysis 分支改为调 `_rerender_shot()`（storyAnalysis 修正后重渲合并工作台）。
- 删 GET 路由的 `/story-analysis.html` 分支（:188-197），落入既有 404。
- docstring :1-17 删 story-analysis.html 相关行，说明 storyAnalysis 修正会重渲 shot-analysis.html。

`src/memoloupe/cli/main.py` `_cmd_validate`：HTML 循环只留 shot-analysis.html，并在其后加残留提示：

```python
    html_path = target / "shot-analysis.html"
    if html_path.is_file():
        issues.extend(validate_html(html_path, root=target, strict=strict))
    legacy_story_html = target / "story-analysis.html"
    if legacy_story_html.is_file():
        issues.append(
            ValidationIssue(
                severity="warning",
                artifact="story-analysis.html",
                json_path="",
                message="旧版残留产物：story-analysis.html 已废弃，故事结果合并呈现在 shot-analysis.html，可删除该文件",
                expected="不存在",
                actual="存在（未校验）",
            )
        )
```

（`ValidationIssue` 需新增 import：`from memoloupe.validate.json_contracts import ValidationIssue`；先核对 `Severity` 类型是否字面量别名，若是 NewType/枚举则按其构造方式传入 `"warning"`。）

`src/memoloupe/analysis/completion.py:43`：`"storyAnalysis": "shot-analysis.html",`（注释同步）。
`src/memoloupe/analysis/profile_aggregate.py:784`：`"storyAnalysisPath": "shot-analysis.html",`。

删除文件：

```bash
git rm src/memoloupe/render/story_html.py templates/story-analysis.html tests/unit/test_story_html.py
```

注意：`tests/unit/story_fixtures.py` 必须保留（test_story_pipeline.py / test_story_model_fill.py / test_profile_pipeline.py 在用）。

- [ ] **Step 4: 运行全部受影响测试**

Run: `.venv/bin/pytest tests/integration/test_story_cli.py tests/integration/test_review_server.py tests/unit/test_profile_aggregate.py tests/unit/test_artifacts.py tests/integration/test_connect_routing.py tests/integration/test_shot_story_chain.py tests/e2e/test_phase2_e2e.py tests/e2e/test_phase3_e2e.py -q`
Expected: 全部通过（e2e 均走 mock，不发起网络请求）

- [ ] **Step 5: 广搜残留引用**

Run: `grep -rn "story_html\|story-analysis\.html" src tests run_*.py pyproject.toml`
Expected: 仅剩 `shot-analysis.html` 字样所在的合法行（如 warning 文案、测试断言、`analysis/completion.py` 注释）与 `docs/`（docs 在 Task 3 处理）；不得有 `render_story_html` 或 `story_html` 模块引用残留。

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: 删除 story-analysis.html 产物链，故事结果只呈现于 shot 工作台"
```

---

### Task 3: 文档同步

**Files:**
- Modify: `AGENTS.md`（实现边界清单）
- Modify: `docs/01_ARCHITECTURE_AND_MODULES.md`（:46、:90、:102、:114、:140、§10 :359-376）
- Modify: `docs/06_DECISIONS_AND_ASSUMPTIONS.md`（D-051 :614-621、D-056 :684-697、新增决策条目）
- Modify: `docs/08_DEVELOPMENT_ROADMAP.md`（:53、:225-227、:235、:268；:93/:100/:102 若为当前状态描述则同步）
- Modify: `README.md`（:16、:35、:47、:66-68、:83）、`README.zh-CN.md`（:16、:34、:64-66、:81）

无测试周期；以 grep 核对为验收。

- [ ] **Step 1: AGENTS.md 实现边界**

「本仓库的输出止于」清单删除 `story-analysis.html` 行，保留 `shot-analysis.html` 与 `style-profile.json`。

- [ ] **Step 2: docs/01 §10 与结构引用**

- §10 命令块（:363-368）删 `memoloupe story --output-dir DIR [options]` 行；`memoloupe shot` 行补注「默认镜头+故事合并流程；--skip-story 只跑镜头；--story-only 只跑故事（承接原 story 命令全部参数）」。
- 兼容包装块（:370-376）删 `python run_story_analysis.py --output-dir DIR` 行。
- :46（目录树 `story_analysis.py`）改为注明「实现模块，由 shot --story-only / 链式流程调用」；:90 删 `story_html.py` 行；:102 删 `story-analysis.html` 行；:114 薄包装句子删 story 部分；:140 服务接口列表中的 `story` 项改为指向 shot 子命令。

- [ ] **Step 3: docs/06 决策更新与新增**

- D-051（:620-621）「`story-analysis.html` 仍作为 Phase 2 契约产物保留……」句改为：story-analysis.html 已废弃（见新决策条目编号），story 结果只呈现在 shot-analysis.html。
- D-056（:690）「corrections 使 story 失效后用独立 `memoloupe story` 重跑」改为「用 `memoloupe shot --story-only` 重跑」。
- 文末新增一条决策（编号取现有最大值 +1，先 grep `docs/06_DECISIONS_AND_ASSUMPTIONS.md` 中 `D-0` 确认）：「CLI 入口统一：删除 memoloupe story 子命令与 story-analysis.html」——日期 2026-09-04，理由（三阶段 CLI 冗余；story 结果已合并呈现于工作台；重跑入口由 shot --story-only 承接；storyAnalysisPath/_HTML_FILES 值改指 shot-analysis.html，schema 不变；validate 对残留旧文件只 warning）。

- [ ] **Step 4: docs/08 与 README**

- docs/08 :53 改为「主流程两条命令；校对后重跑故事用 `memoloupe shot --story-only`」；:225-227 交付清单删 `render/story_html.py` 与 `templates/story-analysis.html` 行、`cli/story_analysis.py` 行注明为实现模块；:235 改写为 `memoloupe shot --story-only`；:268 流程图删「→ story-analysis.html」。
- README.md：:16 表格删 story 行；:66-68 命令表删 `story --output-dir DIR` 行；:35/:47/:83 涉及 story 命令/双 HTML 的措辞改为单工作台表述。README.zh-CN.md 同步（:16、:34、:64-66、:81）。

- [ ] **Step 5: grep 验收**

Run: `grep -rn "memoloupe story\|run_story_analysis\.py\|story-analysis\.html" AGENTS.md README.md README.zh-CN.md docs/00*.md docs/01*.md docs/06*.md docs/08*.md`
Expected: 仅剩余对「已废弃/旧版残留」的历史说明性句子（D-051 改写句、docs/06 新决策条目、validate warning 语义说明）；无现行用法描述。

- [ ] **Step 6: Commit**

```bash
git add AGENTS.md README.md README.zh-CN.md docs/01_ARCHITECTURE_AND_MODULES.md docs/06_DECISIONS_AND_ASSUMPTIONS.md docs/08_DEVELOPMENT_ROADMAP.md
git commit -m "docs: CLI 入口统一与 story-analysis.html 废弃的文档同步"
```

---

### Task 4: 全量回归与真实样例冒烟

**Files:** 无新改动（发现问题回到对应任务修复）

- [ ] **Step 1: 全量测试**

Run: `.venv/bin/pytest -q`
Expected: 全绿（含 e2e；跳过项与基线一致）

- [ ] **Step 2: 真实样例冒烟（副本上进行）**

```bash
rm -rf /tmp/ml-story-smoke && cp -R output/disney-qwen-ui-20260903 /tmp/ml-story-smoke
.venv/bin/memoloupe shot --story-only --output-dir /tmp/ml-story-smoke --scaffold-only --allow-draft
.venv/bin/memoloupe validate /tmp/ml-story-smoke
```

预期：story 阶段完成（scaffold），shot-analysis.html 重渲含故事轨道；validate 退出码 0，且输出含 story-analysis.html 残留 warning（副本里有旧文件）。再 `rm /tmp/ml-story-smoke/story-analysis.html && .venv/bin/memoloupe validate /tmp/ml-story-smoke` 确认 warning 消失、退出码 0。

- [ ] **Step 3: 帮助输出核对**

Run: `.venv/bin/memoloupe --help && .venv/bin/memoloupe shot --help | grep -E "story-only|scaffold-only|mock-text-model|allow-draft"`
预期：主帮助无 story 子命令；shot 帮助含新参数。

---

## Self-Review 记录

- Spec 覆盖：CLI 层（Task 1）、产物层删除 + review server + validate warning（Task 2）、§2.1 契约毗邻点（Task 2 Step 3 的 completion.py / profile_aggregate.py）、文档（Task 3）、验证（Task 4）。
- 不变量核对：storyAnalysis documentType 与 completion 规则保留（Global Constraints + Task 2 不动 rules/schemas）；`run_story_analysis` 可导入性在 Task 1 Produces 声明；story_fixtures.py 保留在 Task 2 Step 3 显式标注；html_contract storyAnalysis 规则明确不动。
- Placeholder 扫描：Task 2 Step 1 的 review server 断言文本「新标题」标注了以用例实际 POST 文本为准——这是该测试文件内既有变量，实现者需读文件确认，可接受；其余代码块均为完整可落稿内容。
- 类型一致性：`run_story_analysis(argv)` 签名跨任务一致；`ValidationIssue` 字段名与 json_contracts.py:33-35 定义一致；e2e/集成测试的 CLI 参数与 Task 1 定义一致。
