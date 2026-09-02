# Shot 工作台暖纸品牌风（render.v3）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `templates/shot-analysis.html` 换成暖纸浅色品牌主题并在页头植入 MemoLoupe logo，DOM 结构、`data-*` 语义、JS 交互与 JSON 契约全部不变。

**Architecture:** 纯呈现层重设计。logo 以静态资产形式在渲染时复制进输出目录 `assets/memoloupe-logo.png`，HTML 相对路径引用（CSP `img-src 'self'` 不变，离线 file:// 与 review server 均可加载）。样式集中在模板 `<style>` 块内替换，Python 侧只动 `_metadata_html` 页头 markup 与资产复制。

**Tech Stack:** Python 3.12 / uv / pytest；原生 HTML/CSS/JS 模板（无构建链）。

**Spec:** `docs/superpowers/specs/2026-09-02-shot-html-warm-paper-brand-design.md`

## Global Constraints

- 不改 `story-analysis.html`、`review_server.py` 行为（server 重渲染复用 `render_shot_html`，自动获得新主题与 logo）。
- 保持不变：`data-document-type="shotAnalysis"` 及全部 `data-document-*`；镜头列 `data-shot-id/start/end/needs-review/review-reasons`；字段单元格 `data-field/value-state/confidence/evidence-refs/source/verified`；corrections 导出/保存/确认交互与 schema。
- CSP 保持 `default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'` 不变。
- 渲染版本从 `render.v2` 升 `render.v3`；`SHOT_RENDER_VERSION` 在 `src/memoloupe/render/shot_html.py:35`。
- 状态语义与文案不变，只换颜色；`absent` 与 `absent-claimed` 视觉差异必须保留。
- 测试命令：`uv run pytest -q`（提交前全量必须绿，当前基线 1158 passed, 8 skipped）。

---

### Task 1: 品牌资产 + 页头 logo 注入 + render.v3

**Files:**
- Create: `assets/brand/memoloupe-logo.png`（由 `assets/readme/source/logo-cropped.png` 复制）
- Modify: `src/memoloupe/render/shot_html.py`（`:35` 版本常量、`_metadata_html` 约 `:1037-1045`、`render_shot_html` 约 `:1085-1203`）
- Test: `tests/unit/test_shot_html.py`（`:145`、`:221-222` 的版本断言 + 新增测试）

**Interfaces:**
- Consumes: `assets/readme/source/logo-cropped.png`（已存在的裁剪版 logo，1674×276）
- Produces: `_LOGO_SOURCE: Path`、`_copy_logo_asset(out_dir: Path) -> None`；渲染产物 HTML 页头含 `<img class="brand-logo" src="assets/memoloupe-logo.png" alt="MemoLoupe" height="28">`；`SHOT_RENDER_VERSION == "render.v3"`

- [ ] **Step 1: 放置品牌资产**

```bash
mkdir -p assets/brand
cp assets/readme/source/logo-cropped.png assets/brand/memoloupe-logo.png
```

- [ ] **Step 2: 写失败测试**

在 `tests/unit/test_shot_html.py` 的 `TestRenderWithoutModelArtifacts` 类（`:89`）中新增（复用文件内已有 `_copy_fixture(tmp_path)` 辅助函数，返回渲染用 out_dir）：

```python
    def test_logo_asset_copied_and_referenced(self, tmp_path):
        work = _copy_fixture(tmp_path)
        from memoloupe.render.shot_html import render_shot_html

        html_path = render_shot_html(work)
        document = html_path.read_text(encoding="utf-8")
        assert (work / "assets" / "memoloupe-logo.png").is_file()
        assert '<img class="brand-logo" src="assets/memoloupe-logo.png" alt="MemoLoupe"' in document

    def test_render_version_is_v3(self):
        from memoloupe.render.shot_html import SHOT_RENDER_VERSION

        assert SHOT_RENDER_VERSION == "render.v3"
```

同时把 `:145` 与 `:221-222` 三处 `render.v2` 断言改为 `render.v3`。

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_shot_html.py -q`
Expected: FAIL（`test_logo_asset_copied_and_referenced`、`test_render_version_is_v3` 及两处 render.v2 断言）

- [ ] **Step 4: 实现 logo 复制与页头注入**

`src/memoloupe/render/shot_html.py`：

1) 版本常量（`:35`）：

```python
SHOT_RENDER_VERSION = "render.v3"
```

2) 文件顶部 `import shutil`（当前未导入），并在 `_TEMPLATE_PATH`（`:39`）附近加：

```python
_LOGO_SOURCE = Path(__file__).resolve().parents[3] / "assets" / "brand" / "memoloupe-logo.png"
```

3) 新增函数（放在 `_full_video_src` 之后）：

```python
def _copy_logo_asset(out_dir: Path) -> None:
    """把品牌 logo 复制到 out_dir/assets/，供 HTML 以相对路径引用（幂等）。"""
    target = out_dir / "assets" / "memoloupe-logo.png"
    if target.is_file() and target.read_bytes() == _LOGO_SOURCE.read_bytes():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_LOGO_SOURCE, target)
```

4) `_metadata_html`（`:1037-1046`）页头 markup 替换为：

```python
    return (
        '<header class="metadata card" id="metadata">'
        '<div class="metadata-topline"><div class="metadata-brand">'
        '<img class="brand-logo" src="assets/memoloupe-logo.png" alt="MemoLoupe" height="28">'
        "<h1>镜头拉片校对台</h1>"
        "</div>"
        '<span class="badge badge-outline">离线可打开</span></div>'
        f'<dl class="metadata-grid">{items}</dl>'
        "</header>"
    )
```

（原 `.metadata-kicker` 段落移除；logo 已承担品牌标识。）

5) 在 `render_shot_html` 写文件之前（`_load_raws(out_dir)` 之后即可）调用：

```python
    _copy_logo_asset(out_dir)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_shot_html.py tests/integration/test_review_server.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add assets/brand/memoloupe-logo.png src/memoloupe/render/shot_html.py tests/unit/test_shot_html.py
git commit -m "feat(render): shot 页头植入品牌 logo，渲染版本升 render.v3"
```

---

### Task 2: 暖纸浅色品牌主题 CSS 重设计

**Files:**
- Modify: `templates/shot-analysis.html`（`:root` 块约 `:10-41`、body 背景约 `:48-53`、全 style 块 `:9-1077`、`.metadata` 系列 `:136-199`）
- Test: `tests/unit/test_shot_html.py`（新增主题断言）

**Interfaces:**
- Consumes: Task 1 的页头 markup（`.metadata-brand` / `.brand-logo` 类名必须一致）
- Produces: 模板内 `color-scheme: light`、`--background: #f7f2e6`、`--brand: #a57100`；不再出现 `#09090b`/`#111113`/`#18181b`/`#27272a`/`#3f3f46`/`#fafafa`/`#a1a1aa` 等深色字面值

- [ ] **Step 1: 写失败测试**

```python
    def test_light_brand_theme_tokens(self, tmp_path):
        work = _copy_fixture(tmp_path)
        from memoloupe.render.shot_html import render_shot_html

        document = render_shot_html(work).read_text(encoding="utf-8")
        assert "color-scheme: light" in document
        assert "--background: #f7f2e6" in document
        assert "--brand: #a57100" in document
        for dark_token in ("#09090b", "#111113", "#18181b", "#27272a", "#3f3f46"):
            assert dark_token not in document
```

（同样加在 `TestRenderWithoutModelArtifacts` 类中。）

Run: `uv run pytest tests/unit/test_shot_html.py::TestRenderWithoutModelArtifacts -q`
Expected: 新测试 FAIL

- [ ] **Step 2: 替换 `:root` 变量块**

`templates/shot-analysis.html` 的 `:root` 整块替换为：

```css
  :root {
    color-scheme: light;
    --background: #f7f2e6;
    --foreground: #1c2333;
    --card: #fffdf8;
    --card-foreground: #1c2333;
    --popover: #fffdf8;
    --popover-foreground: #1c2333;
    --primary: #1c2333;
    --primary-foreground: #fffdf8;
    --secondary: #f1e9d8;
    --secondary-foreground: #1c2333;
    --muted: #f1e9d8;
    --muted-foreground: #5c6472;
    --accent: #f0e6cf;
    --accent-foreground: #1c2333;
    --brand: #a57100;
    --brand-foreground: #fffdf8;
    --destructive: #b3354a;
    --warning: #92610a;
    --success: #2f7d43;
    --warning-border: rgba(146, 97, 10, 0.35);
    --warning-bg: rgba(197, 138, 25, 0.12);
    --success-border: rgba(47, 125, 67, 0.32);
    --success-bg: rgba(63, 155, 91, 0.12);
    --destructive-border: rgba(179, 53, 74, 0.32);
    --purple-border: rgba(124, 82, 189, 0.35);
    --border: #e5dccb;
    --input: #d8cfbd;
    --ring: #a57100;
    --radius: 14px;
    --shadow: 0 18px 50px rgba(28, 35, 51, 0.10);
  }
```

body 背景改为：

```css
    background:
      radial-gradient(circle at top left, rgba(165, 113, 0, 0.10), transparent 34rem),
      linear-gradient(180deg, #f7f2e6 0%, #f3ecdc 58%, #f7f2e6 100%);
```

- [ ] **Step 3: 清扫 style 块内残留深色字面值**

在模板 `:9-1077` 的 style 块内逐项替换（当前共 19 处），按上下文套用：

| 深色字面值 | 替换为 |
|---|---|
| `rgba(244, 244, 245, 0.08)`（背景辉光） | `rgba(165, 113, 0, 0.10)` |
| `rgba(17, 17, 19, 0.92)`（卡片底） | `rgba(255, 253, 248, 0.94)` |
| `#fafafa`（文字/图标色） | `var(--foreground)` |
| `#a1a1aa`（次级文字） | `var(--muted-foreground)` |
| `#27272a`（边框/分隔） | `var(--border)` |
| `#3f3f46`（输入边框） | `var(--input)` |
| `#111113` / `#18181b`（面板底） | `var(--card)` / `var(--popover)` |

替换完成后 `grep -nE "#09090b|#111113|#18181b|#27272a|#3f3f46|#fafafa|#a1a1aa|rgba\(244, 244, 245" templates/shot-analysis.html` 必须无输出。

- [ ] **Step 4: 组件质感调整**

仍在同一 style 块内：

1) 页头品牌区（替换 `.metadata-kicker` 规则，并调整 `.metadata h1`）：

```css
  .metadata-brand {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .brand-logo {
    display: block;
    height: 28px;
    width: auto;
  }
  .metadata h1 {
    margin: 0;
    font-size: 20px;
    line-height: 1.2;
    letter-spacing: -0.01em;
  }
```

2) 选中/激活态统一品牌金：时间线选中镜头、胶片条当前项、Sidebar 激活导航的描边/背景由白色系改为 `var(--brand)`（搜索 `outline`、`border-color`、`box-shadow` 中使用 `--primary` 或白色的选中态规则逐项替换）。

3) 时间码等数字：在时间线镜头时间码与 Sidebar 时间显示的选择器上加 `font-variant-numeric: tabular-nums`（搜索 `time`/`monospace` 定位）。

4) 按钮主操作（保存/确认类）改为藏青实心：`background: var(--primary); color: var(--primary-foreground); border-color: var(--primary);`，hover 用 `filter: brightness(1.12)`。

5) `.metadata` 粘性头部的 `backdrop-filter` 保留，卡片底色已由 Step 3 换浅。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_shot_html.py -q`
Expected: PASS（含新主题断言与全部既有契约断言）

- [ ] **Step 6: Commit**

```bash
git add templates/shot-analysis.html tests/unit/test_shot_html.py
git commit -m "feat(render): shot 工作台暖纸浅色品牌主题"
```

---

### Task 3: 决策记录 + 全量回归 + 人工验收

**Files:**
- Modify: `docs/06_DECISIONS_AND_ASSUMPTIONS.md`（D-052 追加在 D-051 之后）

- [ ] **Step 1: 追加 D-052**

```markdown
### D-052：Shot 工作台暖纸浅色品牌主题（render.v3）

决策：shot-analysis.html 从深色 shadcn 风格切换为与 README hero 一致的暖纸
浅色品牌主题（米白 `#f7f2e6` 底、藏青 `#1c2333` 正文、品牌金 `#a57100`
强调），页头植入 `assets/brand/memoloupe-logo.png`（渲染时复制到输出目录
`assets/memoloupe-logo.png`，相对路径引用，CSP `img-src 'self'` 不变）。
状态语义、文案与全部 `data-*` 机器语义不变，仅颜色、字阶与组件质感调整。

理由：品牌 logo 为深色文字设计，深色页面上不可读；统一浅色品牌主题让
README 与审片工作台视觉一致。story-analysis.html 本次保持现状。
```

- [ ] **Step 2: 全量回归**

Run: `uv run pytest -q`
Expected: 全部通过（基线 1158 passed, 8 skipped + 新增用例）

- [ ] **Step 3: 真实产物重渲染 + 人工验收**

```bash
uv run python -c "from pathlib import Path; from memoloupe.render.shot_html import render_shot_html; print(render_shot_html(Path('output/disney-e2e-20260831')))"
open output/disney-e2e-20260831/shot-analysis.html
```

逐项核对：logo 显示在页头；整体暖纸浅色；时间线/胶片条选中态为金色；字段矩阵、Sidebar、播放器、corrections 导出按钮无视觉回归；`absent` 与 `absent-claimed` 视觉仍有区分。

- [ ] **Step 4: Commit 并等用户指示 push**

```bash
git add docs/06_DECISIONS_AND_ASSUMPTIONS.md
git commit -m "docs: D-052 shot 工作台暖纸品牌主题（render.v3）"
```
