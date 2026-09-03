# shot-analysis 动效时间线多行泳道与页面布局优化设计

日期：2026-09-03
范围：仅 `shot-analysis.html`（模板 `templates/shot-analysis.html` + 渲染器 `src/memoloupe/render/shot_html.py`）。不改 story-analysis.html，不改任何 JSON 数据结构与契约。

## 背景与问题

`shot-analysis.html` 的动效候选轨道（`.motion-event-track`）把所有候选事件用 `position: absolute; top: 11px` 钉在**同一行**（`templates/shot-analysis.html:513-530`），渲染器只为每个事件计算 `left%`/`width%`（`shot_html.py:886-894`）。在 57 秒样例视频上产生 173 个候选时，事件在同一行互相覆盖堆叠，文字完全不可读，只有最上层少数 chip 可点击。

次要问题：三条泳道（故事/动效/镜头）的标签列与间距节奏不统一；点事件（关键帧）与区间事件（变速）视觉形态相同，难以一眼区分。

## 决策记录（用户已确认）

- 动效轨道形态：**多行泳道自动分行**（时间重叠的候选叠到多行子泳道，每个候选可直接点击）。
- 范围：**只改 shot-analysis**，story-analysis 不动。
- 整体打磨为轻量级，不做大范围视觉改版。

## 设计

### 1. 泳道分配（Python 端，确定性）

在 `_motion_timeline_band_html` 渲染前，对事件做贪心区间划分（interval partitioning）：

- 输入：已按 `(startMs, endMs)` 排序的事件列表（现有 `events.sort` 保留）。
- 每个事件换算出显示占用区间：点事件按最小显示宽度（2.8% 轴宽）参与占位，区间事件按 `max(实际时长占比, 3.2%)` 占位，与现有 `width` 计算保持一致。
- 依次把事件放进第一条「该泳道最后事件的占用结束 < 本事件占用开始」的泳道；没有则新开一条。
- 每个事件输出 `--motion-lane: N`（从 0 开始），轨道容器输出 `--motion-lanes: 总泳道数`。
- 纯函数 `_assign_motion_lanes(events)`，无随机、无时间依赖，可单元测试。

### 2. CSS 布局

- `.motion-event-track` 高度由 `--motion-lanes` 撑开：`min-height: calc(var(--motion-lanes) * 30px + 16px)`。
- `.motion-event` 的 `top` 改为 `calc(var(--motion-lane) * 30px + 8px)`；每条泳道高约 26px + 4px 间距。
- 轨道中线（`::before`）随多泳道移除或改为泳道分隔细线。
- 窄屏断点（现有 `@media` 1269 行附近）保持列塌缩行为不变。

### 3. 紧凑模式

- Python 端换算 chip 像素宽度不足（宽度占比低于阈值，初定 4.5%，实现时按实际截图校准）时加 `is-compact` class。
- `is-compact` 下隐藏 `strong`/`span` 文字，只保留色条/圆点；完整信息通过 `title` 悬停提示和点击后右侧镜头详情（现有 `appendMotionEffects` 链路）查看。
- 所有候选仍全部渲染，不做聚合、不隐藏数据。

### 4. 点/区间视觉区分

- 点事件（关键帧）：竖向 tick 标记样式（细竖条 + 小圆点），与区间横条明显区分。
- 区间事件（变速）：保持圆角横条。
- 动效轨道 `timeline-lane-label` 的 `small` 区补充图例说明（如「竖线 = 关键帧 · 横条 = 变速」）。

### 5. 页面轻量打磨

- 三条泳道的 `timeline-lane-label` 列宽、垂直对齐、间距统一。
- 镜头轨道竖排 chip 保持现状，仅统一高度与间距。
- 保持现有暖纸色调与品牌变量，不引入新色板。

## 不变量（不可破坏）

- 不改 JSON 契约、字段、枚举；HTML 只是人工校对视图。
- 所有候选可追溯：点击 chip 仍跳转镜头并在详情抽屉展示候选明细与证据引用。
- 无候选时仍显示「暂无候选」空态；`skipped/failed` 状态标签逻辑不变。
- 渲染保持确定性：同一输入产出同一 HTML。

## 验证

- 新增单元测试：`_assign_motion_lanes` 的重叠事件分行、相邻不重叠事件同行、点事件占位、空列表。
- 回归运行现有 `shot_html` 相关单元/契约测试。
- 用现有样例数据重新生成一份 shot-analysis.html，headless 截图人工核对：动效轨道多行不重叠、紧凑模式生效、点/区间形态可区分、其余区域无回归。
