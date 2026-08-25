# memoclip-lapian vs MemoLoupe 对比与差距分析

> 目的：评估 AI 复刻版 **MemoLoupe** 相对原版 **memoclip-lapian** 的功能完整度，找出复刻缺口。
> 结论先行：**Phase 1（镜头分析）复刻质量很高且语义对齐良好；Phase 2（故事分析）与 Phase 3（风格档案）尚未实现。**

## 版本基线

| 项 | 值 |
|---|---|
| 初版对比基线 | MemoLoupe `eb8b8e2`（M2），749 tests |
| 当前刷新基线 | MemoLoupe `dcee671`（M3），**854 tests 全绿**（2026-08-25 实测 `uv run pytest -q`） |
| 原版基线 | memoclip-lapian `utils/` 23 个脚本（shot 612 行 / story 1197 行 / profile 395 行） |

---

## 1. 一句话结论

| 阶段 | memoclip-lapian | MemoLoupe | 状态 |
|------|:---:|:---:|:---|
| Phase 1 镜头分析 `shot-analysis.html` | ✅ | ✅（854 测试全过） | **基本复刻完成**（含 M3 人工校对闭环） |
| Phase 2 故事分析 `story-analysis.html` | ✅ | ❌ CLI 显式"尚未实现" | **缺失** |
| Phase 3 风格档案 `style-profile.json` | ✅ | ❌ CLI 显式"尚未实现" | **缺失** |

MemoLoupe 现有三个里程碑：`M0+M1: 可执行契约与确定性 Phase 1`、`M2: 音频与智能分析`、`M3: 人工校对`。
原版三阶段产物中，MemoLoupe 已交付 shot-analysis（含原版没有的人工校对），story / profile 未开始。

---

## 2. 工程形态对比（复刻版反而更规范）

| 维度 | memoclip-lapian | MemoLoupe |
|------|----------------|-----------|
| 代码形态 | 平铺脚本 `utils/*.py`，无打包 | 正规包 `src/memoloupe/`，hatchling + uv |
| 依赖 | 无锁定 | `pyproject.toml` + `uv.lock`，仅 jsonschema/numpy |
| 测试 | 仅 `tests/utils/test_concurrency.py` | **854 个测试全过**（unit/contract/integration/e2e） |
| Schema | 无独立 JSON Schema | `schemas/*.json` 13 个契约文件 + jsonschema 校验 |
| 设计文档 | SKILL.md / workflows / README | `docs/00–08` 规格（含 1200 行数据契约原文 + M3–M5 路线图） |
| 命令入口 | 直接跑 `utils/run_*.py` | 统一 `memoloupe` CLI（validate/shot/review/import-corrections；story/profile 未实现） |
| 数据校验 | `validate_html.py` 单文件 | 分层：json_contracts / cross_artifact / html_contract |
| 原子写 | 部分 | `core/atomic_io.py` 临时文件+原子替换（强制规则） |
| 人工校对 | 无 | corrections overlay 四态状态机 + localhost review server + 导入导出 CLI |

**评价**：MemoLoupe 在工程严谨性上**超过**原版——这正是"按架构文档让 AI 重写"的预期收益。

---

## 3. Phase 1 语义对齐度（逐层核对，结果：对齐良好）

### 3.1 五态取值语义 ✅ 完全对齐

原版 `observations.py` 与 MemoLoupe `analysis/observations.py` 都实现：
`value / absent / absent-claimed / unknown / unmapped` 五态 + 独立 `verified` 维度。

MemoLoupe 甚至更严格——用 `__post_init__` 做了构造守卫：
- `state=value` 时 value 不能为 None；`absent/absent-claimed/unknown` 时 value 必须为 None
- `unmapped` 必须保留 `original_value`
- 非 `unknown` 必须带 `evidence_refs`
- `deterministic_absent_observation` 强制 `source ∈ {ffprobe, ffmpeg, audioDetector, appleVision}`，否则 `raise`
- `apply_human_correction` 禁止把非确定性来源人工改成 `absent`

这些是原版散落在注释/校验器里的规则，MemoLoupe 直接做成了**不可构造非法状态的类型约束**。

### 3.2 受控词表 ✅ 对齐

两者都是"加载 vocabulary.json → 归一化 → 三态（value/unmapped/unknown）+ 生成 prompt 词表段"。
MemoLoupe 支持 `allowTransitions`（` → ` 连接）与 `multiValueSeparator`（顿号多选），与原版 `vocabulary.py` 语义一致。

### 3.3 确定性检测器 ✅ 对齐

- **硬切**：原版自研打分（直方图×4.61 + 边缘×3.75 − 5.49）；MemoLoupe `media/shots.py` 用 ffmpeg `fps=…,scale='min(size,iw)':-2` 抽帧打分，思路一致（精确定义见 docs/06 D-018，CALIBRATION）。
- **音频切点**：两者都是 6 特征（rmsDb / zeroCrossingRate / roughness / amplitudeShape / autocorrelation1ms / autocorrelation4ms），权重同为 `(1.0, 0.8, 0.8, 0.6, 0.8, 0.8)`。MemoLoupe 用 numpy 20ms 帧实现（D-020）。
- **BGM 检测**：两者都由确定性检测器回答"有没有"（`music-flags.json`），风格归模型 `bgmStyle`（D-021）。
- 其余 audio_energy / quality / probe / clips / frames / camera_motion 均一一对应。

### 3.4 人工校对（M3 新增，原版没有）

- `schemas/corrections.json` + `render/corrections.py`：纯追加 overlay，不改 raw（D-006）。
- 四态状态机：`draft / underReview / confirmed / outdated`，outdated 优先，confirmed 仅显式动作。
- `analysis/completion.py`：completion 规则评估 + confirm 三道闸门（completion 通过 + strict 校验无 error + 第三道）。
- 校对 UI：词表下拉、verified 切换、pending 防丢、证据抽屉、区间播放、边界修正表单。
- `render/review_server.py` localhost API + `import-corrections` CLI；e2e 覆盖修正保留/重跑保留/revision 变更 outdated/confirm 全链路。

---

## 4. 实质差异（已逐项关闭/定位）

### 4.1 模型分组：2 组 → 3 组 ✅ 已关闭

- **原版**：`visual` + `audio` 两组。
- **MemoLoupe**：`visual` + `audio` + `editing_function` 三组，字段所有权单一事实源在 `services/mock.GROUP_OWNED_SECTIONS`，启动自检、重叠即 raise。
- **结论（docs/08 §2 已关闭的路线分歧）**：三组为内部执行，下游只消费合并后的稳定 `unified-media.json`，不改变对外契约。

### 4.2 `bgm` 字段归属 ✅ 已关闭

- **原版 audio 组含 `bgm`**（模型也答"有没有"，检测器交叉验证）。
- **MemoLoupe audio 组只有 `speech/bgmStyle/soundEffects`**，存在性完全交给 `audio_music.py` 检测器。
- **结论（docs/08 §2 已关闭）**：BGM 存在性只由 `music-flags.json` 确定性检测负责；UnifiedMLLM 只输出 `bgmStyle`。MemoLoupe 方向被确认为正式设计，与"确定性检测优先于模型"不变量一致。

### 4.3 needsReview 渲染接入 🔶 部分完成（M3 范围内唯一遗留）

`render/shot_html.py` 已改用 `build_observations_with_review()`，合并 `shots.json.needsReview` 与 resolver 冲突理由，HTML escape 后以 `title=` 属性展示，并有单测 `test_review_reasons_mark_column_header`。
**剩余缺口**（docs/08 03-01 未完成项）：
1. 缺 `data-review-reasons` 或等价**机器可读稳定 HTML 语义**（当前只有 `title=`）。
2. `html_contract.py` 尚未校验 needs-review 与 reasons 的一致性。
3. 缺 resolver→render→validate 回归测试。

---

## 5. 复刻缺口清单（要达到"完整复刻"需补的）

> 详细拆分见 `MemoLoupe-开发任务清单.md`；MemoLoupe 官方执行路线在 `MemoLoupe/docs/08_DEVELOPMENT_ROADMAP.md`（03/04/05 三 Phase，GSD 结构）。

### 高优先级（整个阶段缺失）
1. **Phase 2 故事分析**（roadmap Phase 03，plans 03-02~03-04）：
   - `analysis/story_pipeline.py`（ASR gap 确定性聚块 + 文本模型填叙事字段，不送视频）
   - `render/story_html.py` + `templates/story-analysis.html`
   - CLI `memoloupe story`（当前显式 `_cmd_not_implemented`）
2. **Phase 3 风格档案**（roadmap Phase 04）：
   - `analysis/profile_aggregate.py`（纯函数确定性聚合：slot 序列/时长占比/镜头时长分布/景别转场光线分布/voiceMix/hosted 占比/音画边界）
   - 模型蒸馏趟 + `style-profile.json` 输出（schema 已有 `schemas/style-profile.json` v2）
   - CLI `memoloupe profile`

### 中优先级（Phase 1 收尾）
3. 03-01 遗留三项（见 §4.3）：`data-review-reasons` 机器语义、html_contract 校验、回归测试。

### 低优先级
4. 真实服务适配（真实 UnifiedMLLM/ASR/文本模型端点，fallback 换模型重发——docs/06 D-023 待办）。
5. 完整 `rules/vocabulary.json`；待校准参数用真实视频标定（docs/06 §4 A-001~A-007）。

---

## 6. 总体判断

MemoLoupe 是一次**高质量的架构驱动复刻**：把原版"脚本集合"重构成"契约先行、类型安全、854 测试覆盖的正规包"，工程严谨性反超原版。**Phase 1 的五态语义、受控词表、确定性检测三大核心都对齐良好**，且 M3 补齐了原版没有的人工校对闭环。

但它**还不是"完整复刻"**——目前交付到 M3（确定性 Phase 1 + 音频/智能分析 + 人工校对），**Phase 2 与 Phase 3 整条流水线缺失**。两处字段级设计分歧（bgm、三组）均已确认为有意设计而非缺陷。要达到与 memoclip-lapian 功能等价，主要工作是补全故事分析与风格档案两个阶段，并收尾 03-01 的三处 needsReview 缺口。
