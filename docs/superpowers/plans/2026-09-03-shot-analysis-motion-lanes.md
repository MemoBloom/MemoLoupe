# shot-analysis 动效时间线多行泳道实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 shot-analysis.html 动效候选轨道从单行绝对定位（173 个候选互相堆叠不可读）改为多行泳道自动分行，并做轻量页面打磨。

**Architecture:** 泳道分配在 Python 渲染端用确定性贪心区间划分完成（无 JS 布局），事件输出 `--motion-lane` CSS 变量；模板 CSS 按 `--motion-lanes` 撑开轨道高度。窄区间 chip 进入紧凑模式（隐藏文字），关键帧点事件改为圆点标记。

**Tech Stack:** Python 3 渲染器（`src/memoloupe/render/shot_html.py`）、内联 CSS 模板（`templates/shot-analysis.html`）、pytest、Edge headless 截图验证。

**Spec:** `docs/superpowers/specs/2026-09-03-shot-analysis-motion-lanes-design.md`

## Global Constraints

- 不改任何 JSON 契约、字段、枚举；HTML 只是人工校对视图。
- 渲染确定性：同一输入产出同一 HTML；泳道分配为纯函数，无随机、无时间依赖。
- 所有候选必须全部渲染并可点击追溯（不聚合、不隐藏数据）。
- 时间一律整数毫秒；镜头区间 `[startMs, endMs)`。
- 所有时间使用整数毫秒；不得静默吞掉未知字段。
- 测试命令统一用 `.venv/bin/pytest`；CLI 用 `.venv/bin/memoloupe`。
- 只改 `templates/shot-analysis.html` 与 `src/memoloupe/render/shot_html.py` 及对应测试；不动 story-analysis。

---

### Task 1: `_assign_motion_lanes` 纯函数

**Files:**
- Modify: `src/memoloupe/render/shot_html.py`（在 `_motion_timeline_band_html` 定义之前插入常量与函数）
- Test: `tests/unit/test_shot_html.py`

**Interfaces:**
- Produces: `_assign_motion_lanes(events: list[dict[str, object]]) -> int`
  - 输入 events 已按 `(startMs, endMs)` 排序，每个 event 必含键：`left: float`（百分比 0–98）、`width: float`（百分比）、`isPoint: bool`。
  - 副作用：给每个 event 写入 `lane: int`（从 0 开始）。
  - 返回：泳道总数（空列表返回 0）。
- 常量：`_MOTION_COMPACT_WIDTH_PCT = 4.5`、`_MOTION_LANE_GAP_PCT = 0.4`、`_MOTION_POINT_OCCUPANCY_PCT = 1.2`。Task 2 消费这些常量与函数。

- [ ] **Step 1: 写失败测试**

在 `tests/unit/test_shot_html.py` 末尾新增（文件顶部 import 区若无则加上 `from memoloupe.render.shot_html import _assign_motion_lanes`）：

```python
class TestAssignMotionLanes:
    def _event(self, left, width, is_point=False):
        return {"left": left, "width": width, "isPoint": is_point}

    def test_empty_events_return_zero_lanes(self):
        assert _assign_motion_lanes([]) == 0

    def test_non_overlapping_events_share_lane_zero(self):
        events = [self._event(0.0, 10.0), self._event(12.0, 10.0)]
        assert _assign_motion_lanes(events) == 1
        assert [e["lane"] for e in events] == [0, 0]

    def test_overlapping_events_spill_to_next_lane(self):
        events = [self._event(0.0, 10.0), self._event(5.0, 10.0)]
        assert _assign_motion_lanes(events) == 2
        assert [e["lane"] for e in events] == [0, 1]

    def test_third_event_reuses_first_lane_when_free(self):
        events = [
            self._event(0.0, 10.0),
            self._event(5.0, 10.0),
            self._event(20.0, 10.0),
        ]
        assert _assign_motion_lanes(events) == 2
        assert [e["lane"] for e in events] == [0, 1, 0]

    def test_point_event_uses_compact_occupancy(self):
        # 点事件按 1.2% 占位：left=2.0 的点事件不占住 left=5.0 之后的泳道。
        events = [self._event(2.0, 2.8, is_point=True), self._event(5.0, 10.0)]
        assert _assign_motion_lanes(events) == 1
        assert [e["lane"] for e in events] == [0, 0]

    def test_gap_prevents_visual_touching(self):
        # 0.4% 间隙：left=10.2 紧跟 right=10.0 但小于 gap，仍分行。
        events = [self._event(0.0, 10.0), self._event(10.2, 5.0)]
        assert _assign_motion_lanes(events) == 2
        assert [e["lane"] for e in events] == [0, 1]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/pytest tests/unit/test_shot_html.py::TestAssignMotionLanes -v`
Expected: FAIL（`ImportError` 或 `cannot import name '_assign_motion_lanes'`）

- [ ] **Step 3: 实现函数**

在 `src/memoloupe/render/shot_html.py` 中 `_motion_shot_for_range` 函数结束之后、`_motion_timeline_band_html` 定义之前插入：

```python
_MOTION_COMPACT_WIDTH_PCT = 4.5
_MOTION_LANE_GAP_PCT = 0.4
_MOTION_POINT_OCCUPANCY_PCT = 1.2


def _assign_motion_lanes(events: list[dict[str, object]]) -> int:
    """把动效事件按显示占用贪心分配到泳道，返回泳道总数。

    每个事件需已带 ``left``/``width``（时间轴百分比）与 ``isPoint`` 键；
    点事件按 ``_MOTION_POINT_OCCUPANCY_PCT`` 占位。函数给每个事件写入
    ``lane`` 键（从 0 开始）。纯函数：无随机、无时间依赖。
    """
    lane_rights: list[float] = []
    for event in events:
        left = float(event["left"])  # type: ignore[arg-type]
        if event["isPoint"]:
            occupancy = _MOTION_POINT_OCCUPANCY_PCT
        else:
            occupancy = float(event["width"])  # type: ignore[arg-type]
        lane = 0
        while lane < len(lane_rights) and left < lane_rights[lane] + _MOTION_LANE_GAP_PCT:
            lane += 1
        if lane == len(lane_rights):
            lane_rights.append(0.0)
        lane_rights[lane] = left + occupancy
        event["lane"] = lane
    return len(lane_rights)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/pytest tests/unit/test_shot_html.py::TestAssignMotionLanes -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/memoloupe/render/shot_html.py tests/unit/test_shot_html.py
git commit -m "feat: 动效事件泳道贪心分配函数"
```

---

### Task 2: `_motion_timeline_band_html` 接入泳道与紧凑模式

**Files:**
- Modify: `src/memoloupe/render/shot_html.py`（`_motion_timeline_band_html`，当前 863–934 行区域）
- Test: `tests/unit/test_shot_html.py`（`TestMotionEffectsRendering` 更新 + 新增多泳道用例）

**Interfaces:**
- Consumes: Task 1 的 `_assign_motion_lanes`、`_MOTION_COMPACT_WIDTH_PCT`。
- Produces: HTML 契约（供 Task 3 的 CSS 与人工截图核对）：
  - 轨道容器：`<div class="motion-event-track" role="list" style="--motion-lanes: N">`（无候选时 N=1）。
  - 事件 chip：`style` 含 `--motion-left`、`--motion-width`、`--motion-lane`；class 为 `motion-event shot-jump is-point`（点事件）或 `motion-event shot-jump is-range` / `motion-event shot-jump is-range is-compact`（窄区间）。
  - 泳道标签区新增图例：`<small class="motion-lane-legend">圆点 = 关键帧 · 横条 = 变速区间</small>`。

- [ ] **Step 1: 更新与新增失败测试**

`tests/unit/test_shot_html.py` 中 `test_motion_effects_summary_and_sidebar_context_render` 的断言更新——把：

```python
        assert 'class="motion-event shot-jump is-range"' in html
```

改为（125ms 的 ramp 在夹具时间轴上宽度占比 < 4.5%，必为紧凑模式）：

```python
        assert 'class="motion-event shot-jump is-range is-compact"' in html
```

并在同一测试追加：

```python
        assert "圆点 = 关键帧" in html
        assert "--motion-lanes: 1" in html
        assert "--motion-lane: 0" in html
```

新增多泳道渲染用例（复制该测试的夹具写法，`speedRamps` 放两条时间重叠的 ramp）：

```python
    def test_overlapping_speed_ramps_render_on_separate_lanes(self, tmp_path):
        work = _copy_fixture(tmp_path)
        motion_path = work / "raw" / "motion-effects.json"
        motion = json.loads(motion_path.read_text(encoding="utf-8"))
        motion["status"] = "complete"
        ramp = {
            "type": "impact_cut",
            "durationMs": 400,
            "avgMotion": 0.3,
            "confidence": "medium",
            "evidence": "cut_score peak=0.34",
            "replicationHint": "Use a 2-4 frame exposure hit.",
            "needsVisualConfirmation": True,
            "evidenceRefs": ["raw/motion-effects.json#frameMetrics[0]"],
        }
        motion["speedRamps"] = [
            {**ramp, "startMs": 1000, "endMs": 1400},
            {**ramp, "startMs": 1200, "endMs": 1600},
        ]
        motion_path.write_text(json.dumps(motion, ensure_ascii=False), encoding="utf-8")

        out = render_shot_html(work)
        assert _errors(out, work) == []
        html = out.read_text(encoding="utf-8")

        assert "--motion-lanes: 2" in html
        assert "--motion-lane: 1" in html
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/pytest tests/unit/test_shot_html.py::TestMotionEffectsRendering -v`
Expected: FAIL（`--motion-lanes` 等尚未输出）

- [ ] **Step 3: 修改渲染器**

在 `src/memoloupe/render/shot_html.py` 的 `_motion_timeline_band_html` 中，`events.sort(key=lambda event: (int(event["startMs"]), int(event["endMs"])))` 之后插入显示几何预计算与泳道分配：

```python
    for event in events:
        start_ms = int(event["startMs"])
        end_ms = int(event["endMs"])
        duration = max(end_ms - start_ms, 0)
        event["left"] = max(
            0.0,
            min(98.0, ((start_ms - int(first_start)) / timeline_ms) * 100),
        )
        event["width"] = (
            2.8
            if event["isPoint"]
            else max((duration / timeline_ms) * 100, 3.2)
        )
        event["isCompact"] = (not event["isPoint"]) and float(event["width"]) < _MOTION_COMPACT_WIDTH_PCT
    lane_count = _assign_motion_lanes(events)
```

泳道标签区增加图例（`parts` 列表中 `f'<small>{len(events)} 个候选 · ...'` 一行之后）：

```python
        '<small class="motion-lane-legend">圆点 = 关键帧 · 横条 = 变速区间</small>',
```

轨道容器改为输出泳道数：

```python
        f'<div class="motion-event-track" role="list" '
        f'style="--motion-lanes: {max(lane_count, 1)}">',
```

事件渲染循环中，删除原有的 `left`/`width` 局部计算（已上移），改为读取预计算值并输出 `is-compact` 与 `--motion-lane`：

```python
    for event in events:
        start_ms = int(event["startMs"])
        end_ms = int(event["endMs"])
        left = float(event["left"])
        width = float(event["width"])
        lane = int(event["lane"])
        shot = event.get("shot")
        # …（shot_id / clip_src / src_attr / shot_attr / start_attr / end_attr /
        #    disabled / evidence_refs / range_text 各段保持原样不变）…
        kind_class = "is-point" if event["isPoint"] else "is-range"
        if event["isCompact"]:
            kind_class += " is-compact"
        # …（title_bits / summary 保持原样）…
        parts.append(
            f'<button type="button" class="motion-event shot-jump {kind_class}" '
            f'role="listitem" style="--motion-left: {left:.2f}%; '
            f'--motion-width: {width:.2f}%; --motion-lane: {lane}" '
            # …（其余属性与内容保持原样不变）…
        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/pytest tests/unit/test_shot_html.py -v`
Expected: 全部通过（含既有用例无回归）

- [ ] **Step 5: 回归相邻测试**

Run: `.venv/bin/pytest tests/integration/test_cli_html_validate.py tests/integration/test_motion_effects_pipeline.py -v`
Expected: 全部通过

- [ ] **Step 6: Commit**

```bash
git add src/memoloupe/render/shot_html.py tests/unit/test_shot_html.py
git commit -m "feat: 动效轨道输出泳道变量与紧凑模式"
```

---

### Task 3: 模板 CSS 多行泳道与轻量打磨

**Files:**
- Modify: `templates/shot-analysis.html`（`.motion-event-track` / `.motion-event` 区块，当前 411–435、495–576 行区域；`.timeline-lane-label` 420–435 行区域）

**Interfaces:**
- Consumes: Task 2 的 HTML 契约（`--motion-lanes`、`--motion-lane`、`is-compact`、`motion-lane-legend`）。
- Produces: 最终视觉形态，供 Task 4 截图核对。

- [ ] **Step 1: 改 `.motion-event-track`（多行高度 + 泳道分隔线，替换原 495–512 行规则，含删除 `::before` 中线）**

```css
  .motion-event-track {
    --lane-height: 26px;
    position: relative;
    min-height: calc(var(--motion-lanes, 1) * var(--lane-height) + 12px);
    overflow: hidden;
    border: 1px solid var(--border);
    border-radius: 12px;
    background:
      repeating-linear-gradient(
        180deg,
        transparent 0,
        transparent calc(var(--lane-height) - 1px),
        rgba(229, 220, 203, 0.55) calc(var(--lane-height) - 1px),
        rgba(229, 220, 203, 0.55) var(--lane-height)
      ),
      linear-gradient(180deg, rgba(255, 253, 248, 0.72), rgba(241, 233, 216, 0.72));
  }
```

完整删除 `.motion-event-track::before { … }` 规则。

- [ ] **Step 2: 改 `.motion-event` 系列（单行 chip、泳道定位、点事件圆点、紧凑模式，替换原 513–563 行规则）**

```css
  .motion-event {
    position: absolute;
    left: var(--motion-left);
    top: calc(var(--motion-lane, 0) * var(--lane-height) + 3px);
    display: flex;
    align-items: center;
    gap: 6px;
    width: min(var(--motion-width), calc(100% - var(--motion-left)));
    height: 20px;
    min-height: 0;
    overflow: hidden;
    border-color: var(--warning-border);
    border-radius: 999px;
    background:
      linear-gradient(135deg, rgba(197, 138, 25, 0.24), rgba(255, 253, 248, 0.96));
    color: var(--warning);
    padding: 0 8px;
    text-align: left;
    box-shadow: 0 4px 10px rgba(146, 97, 10, 0.10);
  }
  .motion-event.is-point {
    width: 14px;
    min-width: 14px;
    padding: 0;
    background: var(--warning);
    border-color: rgba(146, 97, 10, 0.58);
  }
  .motion-event.is-point strong,
  .motion-event.is-point span,
  .motion-event.is-compact strong,
  .motion-event.is-compact span {
    display: none;
  }
  .motion-event.is-range { min-width: 18px; }
  .motion-event.is-compact { padding: 0 4px; }
  .motion-event:hover:not(:disabled) {
    border-color: rgba(146, 97, 10, 0.58);
    background:
      linear-gradient(135deg, rgba(197, 138, 25, 0.30), rgba(255, 253, 248, 0.98));
  }
  .motion-event.is-point:hover:not(:disabled) { background: #7d5208; }
  .motion-event.is-selected {
    outline: 2px solid var(--brand);
    outline-offset: 2px;
  }
  .motion-event strong {
    flex: 0 0 auto;
    font-size: 11px;
    font-weight: 720;
  }
  .motion-event span {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--muted-foreground);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 10px;
  }
```

注意同时删除旧的 `.motion-event strong, .motion-event span { display: block; … }` 合并规则与 `.motion-event span { margin-top: 3px; … }` 旧规则（被上面新规则替代）。

- [ ] **Step 3: 泳道标签顶对齐 + 图例样式（改 `.timeline-lane-label`，新增 `.motion-lane-legend`）**

`.timeline-lane-label` 的 `justify-content: center;` 改为：

```css
  .timeline-lane-label {
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
    gap: 3px;
    padding-top: 6px;
    color: var(--muted-foreground);
    font-size: 11px;
  }
```

新增：

```css
  .motion-lane-legend { display: block; margin-top: 2px; }
```

- [ ] **Step 4: 空态位置微调**

`.motion-event-empty` 的 `margin: 14px 0 0 10px;` 改为 `margin: 6px 0 0 10px;`（轨道高度变小后保持视觉居中）。

- [ ] **Step 5: 重渲样例并截图核对**

```bash
.venv/bin/memoloupe shot video/disney.MP4 --output-dir output/disney-qwen-ui-20260903 --render-only
"/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" --headless --disable-gpu \
  --screenshot=/tmp/shot-analysis-lanes.png --window-size=1600,2400 --hide-scrollbars \
  "file://$PWD/output/disney-qwen-ui-20260903/shot-analysis.html"
```

用 ReadMediaFile 查看 `/tmp/shot-analysis-lanes.png`，并裁切动效轨道区域细查。预期：
- 动效候选分布在多行泳道，互不重叠；
- 点事件为实心圆点、区间事件为胶囊条、窄区间无文字；
- 左侧标签区顶对齐并显示图例；
- 故事/镜头泳道与胶片条无视觉回归。

若紧凑阈值视觉不理想，微调 `_MOTION_COMPACT_WIDTH_PCT`（4.0–6.0 区间）并重跑本步。

- [ ] **Step 6: Commit**

```bash
git add templates/shot-analysis.html
git commit -m "feat: 动效时间线多行泳道样式与页面打磨"
```

---

### Task 4: 全量回归与最终核对

**Files:**
- 无新改动（仅在发现问题时回到对应任务修复）

- [ ] **Step 1: 全量测试**

Run: `.venv/bin/pytest tests/unit tests/contract tests/integration -q`
Expected: 全部通过。注意 `test_cli_html_validate.py` 会对 HTML 做跨文件引用校验，若因标记变更失败，回到 Task 2/3 修正。

- [ ] **Step 2: 最终截图核对**

重跑 Task 3 Step 5 的截图命令，确认最终状态与预期一致；检查窄屏（`--window-size=1000,2000`）下三条泳道塌缩为单列且无横向溢出。

- [ ] **Step 3: 更新 spec 校准值**

若 Task 3 Step 5 调整了 `_MOTION_COMPACT_WIDTH_PCT`，把 `docs/superpowers/specs/2026-09-03-shot-analysis-motion-lanes-design.md` 中「初定 4.5%」更新为最终值并提交：

```bash
git add docs/superpowers/specs/2026-09-03-shot-analysis-motion-lanes-design.md
git commit -m "docs: 校准动效紧凑模式阈值"
```

---

## Self-Review 记录

- Spec 覆盖：泳道分配（Task 1）、CSS 布局（Task 3 Step 1/2）、紧凑模式（Task 2 + Task 3 Step 2）、点/区间区分（Task 2 class 契约 + Task 3 Step 2）、图例（Task 2 + Task 3 Step 3）、泳道标签统一（Task 3 Step 3）、验证（Task 3 Step 5 + Task 4）。
- 不变量覆盖：候选全量渲染（无聚合逻辑引入）、JSON 契约零改动（只动渲染器与模板）、确定性（纯函数 + 排序在前）。
- 类型一致性：Task 2 消费 Task 1 的 `_assign_motion_lanes` 与 `_MOTION_COMPACT_WIDTH_PCT`，键名 `left`/`width`/`isPoint`/`isCompact`/`lane` 在任务间一致；Task 3 消费 Task 2 的 CSS 变量与 class 名，一致。
