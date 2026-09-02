# Shot 工作台暖纸品牌风重设计（render.v3）

日期：2026-09-02
状态：已批准（brainstorming 两轮问答后确认）
范围：`templates/shot-analysis.html`（shot 审片工作台）单页；`story-analysis.html` 本次不动。

## 背景与目标

D-050 将 shot-analysis.html 重设计为深色 shadcn 风格审片工作台，结构与交互已稳定。
当前问题：页面视觉与 MemoLoupe 品牌（藏青 + 金色 logo、README hero 暖纸浅色主题）
不一致，且 logo 是深色文字设计，直接放在深色页面上不可读。

目标：在**保持 DOM 结构、`data-*` 机器语义、JS 交互与 raw/schema 契约完全不变**
的前提下，把 shot-analysis.html 换成与 README hero 一致的暖纸浅色品牌主题，
并把真实 logo 植入页头。

## 非目标

- 不改 `story-analysis.html`（保持现有样式）。
- 不改任何字段渲染逻辑、corrections 流程、review server 行为。
- 不引入构建链、CDN、外部字体或新运行时依赖（延续 D-050 的原生离线原则）。
- 不改 `raw/*.json` 输入与 JSON 契约。

## 设计

### 1. Logo 植入

- 源资产：复用 `assets/readme/source/logo-cropped.png`（已从根目录
  `memoloupe-logo.png` 裁剪，藏青 + 金色，透明底上的白色闪光点在米白背景上
  自然融合）。
- 分发：`shot_html.py` 渲染时把 logo 复制到输出目录 `assets/memoloupe-logo.png`
  （与 frames/clips 同级的静态资产机制）；HTML 中以相对路径
  `assets/memoloupe-logo.png` 引用。CSP `img-src 'self'` 无需改动，
  离线 file:// 打开与 localhost review server 均可加载。
- 页头 markup：左侧 `<img>`（显示高度约 28px，`alt="MemoLoupe"`）+
  副标题"镜头拉片校对台"文本；右侧文档状态 badge 位置与语义不变。
  logo 加载失败时（资产缺失）页面功能不受影响，仅无图。

### 2. 调色板（对齐 README hero）

| 角色 | 值 |
|---|---|
| `--background` | `#f7f2e6`（暖纸） |
| 卡片底 | `#fffdf8` |
| 正文 `--foreground` | `#1c2333`（藏青） |
| 次级文字 `--muted-foreground` | `#5c6472` |
| 品牌 accent（金） | `#a57100` |
| 边框 `--border` | `#e5dccb` |
| `--input` | `#d8cfbd` |

- `color-scheme: light`（替换现有 `dark`）。
- 语义色全部换成浅色版：warning / success / destructive / purple 使用
  低饱和浅底 + 深色文字 + 同色系边框；具体色值在实现时从现有 rgba 语义色
  做浅色等价换算，**状态语义与文案不变**。
- 选中/强调态统一用品牌金（时间线选中镜头描边、激活导航等）。

### 3. 排版与组件质感

- 字体栈不变（系统字体栈）；只调整字阶、行高与间距节奏。
- 镜头时间码等数字使用 `font-variant-numeric: tabular-nums`。
- 卡片：白底 + 1px 暖色边框 + 柔和投影（替代现有深色厚投影）。
- 按钮：默认白底描边；主按钮藏青实心白字；hover/focus 态浅色等价。
- 时间线与胶片条：轨道改暖灰底，选中镜头金色描边；缩略图边框随主题。
- 粘性播放器与右侧 Sidebar 背景同步浅色化；滚动条、表单控件颜色随
  `color-scheme: light` 自然切换。

### 4. 技术约束与改动面

- 主要改动：`templates/shot-analysis.html` 的 `<style>` 与页头 markup。
- `src/memoloupe/render/shot_html.py`：仅新增 logo 资产复制 + 页头 logo
  引用注入，属最小改动；不改任何字段渲染与 JSON 注入逻辑。
- 必须保持不变：
  - `data-document-type="shotAnalysis"` 及全部 `data-document-*` 属性
  - 镜头列 `data-shot-id/start/end/needs-review/review-reasons`
  - 字段单元格 `data-field/value-state/confidence/evidence-refs/source/verified`
  - corrections 导出/保存/确认交互与 schema
- 渲染版本升 `render.v3`；`docs/06_DECISIONS_AND_ASSUMPTIONS.md` 追加
  决策记录（D-052）。

### 5. 测试与验收

- `tests/unit/test_shot_html.py`：
  - 断言输出 HTML 含 logo 引用（`assets/memoloupe-logo.png`）；
  - 断言 `color-scheme: light` 与新品牌色值存在；
  - 更新受调色板影响的既有样式断言；
  - 断言 logo 文件确实被复制到输出目录。
- 既有契约断言（data-* 语义、字段矩阵、双模式保存）必须全部保持绿色。
- 人工验收：用现有 Disney 产物重新渲染 shot-analysis.html，浏览器打开
  核对：logo 显示、浅色主题、时间线/Sidebar/字段矩阵/播放器均无视觉回归，
  corrections 导出与保存流程正常。

## 风险

- 浅色化后某些低对比状态色（如 absent-claimed）可能不够醒目——实现时逐个
  状态目检，必要时加深文字色而非改变语义。
- 输出目录若已有旧版无 logo 的 HTML 不受影响；新渲染才带 logo 资产。
