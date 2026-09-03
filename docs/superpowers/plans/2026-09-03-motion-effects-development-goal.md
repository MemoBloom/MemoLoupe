# Phase 05-07：motion-effects 运动复刻候选检测开发目标

日期：2026-09-03  
状态：拟议开发目标  
来源：L7 运动复刻参数检测规格 + MemoLoupe 当前 Phase 05 架构  
建议执行方式：拆分给其他开发工具 / 独立 Codex task 实现，本线程负责架构把关与验收 review。

## 1. 目标

在 MemoLoupe 现有 Phase 1 中新增一个确定性 raw artifact：

```text
raw/motion-effects.json
```

该 artifact 吸收 L7 的“动效复刻参数候选”能力，用于从最终视频像素中发现可人工复核的后期动效候选，包括：

- 曲线变速候选：slow / freeze / high-motion / impact beat；
- 关键帧位移候选：position；
- 缩放候选：scale；
- 冲击震动候选：shake；
- 闪白 / 黑场卡点候选：exposure / opacity。

第一版目标是产出可追溯、可校验、可缓存、可在 shot 工作台中查看的候选证据，而不是生成确定复刻参数或修改下游剪辑决策。

## 2. 产品边界

所有 motion-effects 结论都必须被表达为“最终像素推断候选”，不是剪辑工程真值。

强制约束：

- 每个候选必须携带 `needsVisualConfirmation=true`。
- 每个候选必须携带 `confidence` 和一个或多个 `evidenceRefs`。
- 候选不得覆盖 `camera-motion.json` 的摄影机运动判断。
- 候选不得覆盖 `quality-flags.json` 的质量风险判断。
- 候选不得把 `high_motion_region` 直接写成快放。
- 模型声称“无动效”不得形成确定性 `absent`。
- 第一版不直接写入 `style-profile.json`。

建议语义：`motion-effects.json` 是 Phase 1 的 raw 证据层，HTML 只做待复核展示；是否采纳为复刻建议，留给后续人工校对和 profile 增量设计。

## 3. 架构落位

新增或修改的主要模块：

- `schemas/motion-effects.json`
- `src/memoloupe/media/motion_effects.py`
- `src/memoloupe/artifacts/schemas.py`
- `src/memoloupe/analysis/shot_pipeline.py`
- `src/memoloupe/cli/shot_analysis.py`
- `src/memoloupe/validate/cross_artifact.py`
- `src/memoloupe/render/shot_html.py`
- `tests/unit/test_motion_effects.py`
- `tests/contract/test_schema_contracts.py`
- `tests/contract/test_cross_artifact.py`
- `tests/unit/test_shot_pipeline.py`

Pipeline 建议位置：

```text
detect_quality
  -> detect_motion_effects
  -> unified_media_analysis
  -> analyze_camera_motion
```

该步骤只依赖 `media.json`、源视频和分析范围；按镜头聚合时读取 `shots.json` 的 final 区间。缓存指纹建议包含：

```text
source revision
+ analyzed range
+ motionEffects config
+ algorithm version
```

如果按镜头聚合结果严格依赖 final 边界，也可额外纳入 `shots_fp_eff`；但原始全轨 `frameMetrics` 应保持可独立复用。

## 4. 第一版输出契约

`raw/motion-effects.json` 建议包含：

- `schemaVersion`
- `status`
- `analysis`
- `frameMetrics`
- `speedRamps`
- `keyframeCandidates`
- `digest`
- `shots`

`analysis` 至少记录：

- `method`
- `algorithmVersion`
- `sourceRevisionID`
- `durationMs`
- `analyzedRange`
- `sampleFps`
- `sampleWidth`
- `sampleHeight`
- `frameCount`
- `thresholds`
- `limitations`

`frameMetrics` 是逐采样帧对信号：

- `diff`
- `motionEnergy`
- `brightness`
- `brightnessDelta`
- `repeatScore`
- `cutScore`
- `dxPxSample`
- `dyPxSample`
- `scaleRatio`
- `zoomScore`
- `shakeScore`

`speedRamps` 是区域候选：

- `low_motion_or_freeze`
- `high_motion_region`
- `impact_cut`

`keyframeCandidates` 是点候选：

- `position`
- `scale`
- `exposure_or_opacity`
- `shake`

`digest.items` 只取 Top 12 高价值候选供报告/HTML 摘要展示，低置信候选保留在 raw 中。

## 5. 检测算法范围

第一版采用纯确定性、无模型、无 OpenCV 实现：

- ffmpeg 抽样帧；
- 统一缩放到 96x54 灰度小图；
- NumPy 计算帧间差分、亮度差、边缘加权运动能量；
- 暴力块匹配估计全局平移；
- 五档缩放假设检验估计 scale；
- 平移二阶差分估计 shake；
- 分位数自适应阈值检测事件；
- cut guard 抑制切点附近的 position / scale / shake 假阳性；
- exposure 允许发生在切点附近。

默认配置建议：

```json
{
  "motionEffects": {
    "sampleFps": 8.0,
    "sampleWidth": 96,
    "sampleHeight": 54,
    "frameExtractWidth": 480,
    "maxTranslationPx": 8,
    "minimumRegionMs": 250
  }
}
```

短闪白、枪口火光、2-4 帧震动等素材可通过提高 `sampleFps` 校准；默认值保持性能友好。

## 6. 工具分工

### 6.1 契约工具 / agent

负责：

- 新增 `schemas/motion-effects.json`；
- 新增 minimal / full fixtures；
- 更新 `ArtifactName`；
- 更新 schema contract tests；
- 添加非法变体测试。

验收：

- minimal 和 full fixture 均通过 schema；
- 缺 required 字段失败；
- 非法 `shotID` 失败；
- 非法 property / type / confidence 失败；
- `needsVisualConfirmation=false` 被拒绝。

### 6.2 算法工具 / agent

负责：

- 新增 `src/memoloupe/media/motion_effects.py`；
- 实现平移、缩放、shake、亮度突变、区域分组；
- 实现 `build_motion_effects_stub`；
- 添加纯函数单元测试。

验收：

- 合成低运动片段能产生 freeze / low-motion 候选；
- 合成亮度突变能产生 exposure / impact 候选；
- 合成平移序列能产生 position 候选；
- 合成二阶平移突变能产生 shake 候选；
- 所有候选都带 evidenceRefs 和 `needsVisualConfirmation=true`。

### 6.3 Pipeline 工具 / agent

负责：

- 接入 `ShotAnalysisPipeline`；
- 增加 `detect_motion_effects` 步骤；
- 增加 `--skip detect_motion_effects`；
- 确保 `--dry-run` 会写 skipped stub；
- 接入 fingerprint、manifest、report、artifact list。

验收：

- 首次运行写出 `raw/motion-effects.json`；
- 第二次运行复用该 artifact；
- `--force detect_motion_effects` 只强制该步骤；
- `--skip detect_motion_effects` 写 `status=skipped`，不暗示没有动效；
- optional step 失败时 pipeline 为 partial，但后续 render / validate 继续。

### 6.4 校验与 HTML 工具 / agent

负责：

- cross-artifact 校验 shot 覆盖；
- 校验候选 `shotID` 存在于 `shots.json`；
- 校验 `evidenceRefs` 可解析；
- 在 shot 工作台总览和右侧 Sidebar 展示候选数量、类型和待视觉确认；
- 不把候选渲染成可编辑的确定 Observation。

验收：

- 坏 shotID 会被 strict validate 报错；
- 坏 evidenceRef 会被 strict validate 报错；
- `shot-analysis.html` 可见“运动复刻候选 / 待确认”；
- 页面保留全部既有 `data-*` 语义，不破坏 HTML contract。

### 6.5 文档 / 决策工具 / agent

负责：

- 更新 `docs/06_DECISIONS_AND_ASSUMPTIONS.md`；
- 更新 `docs/07_SOURCE_DATA_CONTRACT.md`；
- 更新 `docs/08_DEVELOPMENT_ROADMAP.md`；
- 新增校准项 `A-008 motion-effects`。

验收：

- 文档明确 motion-effects 与 camera-motion / quality-flags 的边界；
- 文档明确所有候选是视觉推断；
- 文档明确第一版不进入 profile；
- 文档记录算法版本、缓存影响和局限。

## 7. 完成定义

第一版完成时至少通过：

```bash
uv run pytest -q tests/unit/test_motion_effects.py
uv run pytest -q tests/unit/test_shot_pipeline.py
uv run pytest -q tests/contract/test_schema_contracts.py
uv run pytest -q tests/contract/test_cross_artifact.py
```

并完成一个小合成视频验证：

```bash
uv run memoloupe shot video/synthetic-motion.mp4 --output-dir output/motion-effects-smoke --dry-run
uv run memoloupe validate output/motion-effects-smoke --strict
```

其中 `--dry-run` 可接受其他模型相关产物为 skipped，但 `motion-effects` 的 skip/运行语义必须清晰可见。

## 8. 暂不做

第一版不做：

- 不把候选写入 `style-profile.json`；
- 不生成 Story Spine；
- 不做用户素材匹配；
- 不输出 FCPXML；
- 不用模型判定动效存在；
- 不把局部字幕动画作为稳定检测目标；
- 不检测 rotation；
- 不声称能区分摄影机运动、主体运动和后期合成运动。

## 9. 后续增量

后续可在 raw + HTML 稳定后追加：

- review server 中对 motion-effects 候选的人工确认 overlay；
- profile 中的 `adoptionHints.motionEffects` 聚合；
- motion-effects 黄金样例校准；
- 与 quality freeze / black / exposure 信号的共享证据呈现；
- 与 camera-motion 的冲突提示。
