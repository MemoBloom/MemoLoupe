# 设计决策、假设与待校准项

本文件防止开发者把推断误认为原始协议。实现过程中如获得新设计信息，应更新本文件并评估 schema、缓存和迁移影响。

## 1. 已确认事实

- 项目从空仓库复现，没有可供逐行参考的原始源码。
- 产品有 Phase 1 镜头分析、Phase 2 故事分析、Phase 3 风格档案。
- 产品边界止于 style profile。
- 稳定数据契约为 1.0；style profile schema 为 2。
- JSON 是机器主契约，HTML 是人工校对视图。
- 五态语义和模型“无”降级是强约束。
- 每个值必须可追溯。
- detected 与 final 镜头边界并存。
- 代表帧不发送给统一视频模型，模型使用带音频 clip。
- UnifiedMLLM 支持批量、并发、重试、fallback 和 checkpoint。
- Phase 2 主要使用文本摘要，不发送视频。
- Profile 先确定性聚合，再模型蒸馏。
- 下游按故事插槽 affordance 匹配，不做具体语义一一复刻。

## 2. 当前设计决策

### D-001：物理输出路径

决策：稳定逻辑名映射到 `output-dir/raw/<name>.json`；HTML 和 style profile 在根目录。  
理由：兼容原架构的 raw 分层，同时保留稳定文件名。  
兼容：旧 `raw/shot-candidates.json` 由 ArtifactStore/manifest 识别。

### D-002：Python 包布局

决策：使用 `src/memoloupe` 包，根级 run 脚本仅作薄包装。  
理由：避免脚本间循环依赖，便于测试和发布。

### D-003：故事边界默认来源

决策：默认 `asr-gap`，模型边界为可选。  
理由：原架构强调确定性聚块；数据协议允许 model/asr-gap。

### D-004：模型分组与批次

决策：三组是内部字段所有权，batch 是传输与重试单元；最终合并为统一契约。  
理由：两份材料分别强调 groups 和 batches，二者不冲突。

### D-005：运镜证据合并

决策：Apple Vision 和模型结果同时保留，resolver 不静默覆盖；冲突触发 needsReview。  
理由：Vision 测的是图像运动，模型给的是摄影语义，两者认识论不同。

### D-006：人工修正旁路

决策：使用 corrections overlay，不修改 raw。  
理由：保证原始证据可追溯和可重新渲染。

### D-007：无音轨与静音

决策：无音轨为 unavailable/unknown，不等于检测到静音。  
理由：缺少信号与存在静音是不同事实。

### D-008：帧 range

决策：帧允许 start=end，明确视为时间点证据。  
理由：协议示例如此，而普通媒体区间仍是半开区间。

### D-009：NumPy

决策：音频 STFT 和数值运算允许 NumPy；ffmpeg/ffprobe 仍是媒体解码和确定性信号来源。  
理由：数据协议明确给出 NumPy STFT 方法，且纯 Python 大音频计算成本高。

### D-010：模型不可用时继续

决策：模型能力默认非致命；生成 unknown/scaffold/确定性 profile。  
理由：可播放、可校对和失败可见优先。

### D-011：默认 HTML 离线

决策：不使用 CDN 和外链脚本，媒体使用相对路径。  
理由：便于归档、校对和安全验证。

### D-012：模型代理与证据 clip 分离

决策：证据 clip 保留准确镜头区间；模型代理可以补时长、缩放和转码。  
理由：模型兼容处理不得污染事实边界。

### D-013：Schema-first 契约实现（M0）

决策：`schemas/*.json`（JSON Schema Draft 2020-12）是字段契约唯一来源，运行时用 `jsonschema` 库校验；raw JSON 以"校验过的 dict"流通，不维护镜像数据类。数据类仅用于语义层对象（Observation、ID、配置）。  
理由：满足 docs/01 §6"schema 能从同一来源生成或与数据类做一致性测试"，避免双份定义漂移。  
影响：契约测试直接锁定 schema 与 fixtures；字段变更只改 schema + fixtures + 迁移。

### D-014：M0 校验器范围（M0）

决策：M0 交付 JSON schema 校验（`validate/json_contracts.py`）、跨文件校验（`validate/cross_artifact.py`）与 `memoloupe validate` CLI；HTML 语义校验器随 M1/M3 模板交付。  
理由：M0 时尚无 HTML 产物可校验。

### D-015：扩展状态枚举落地（M0）

决策：C-004 与 C-005 按既定方向落地——`music-flags.json` 的 `status` 枚举扩展为 `complete/partial/unavailable/skipped/failed`；`camera-motion.json` 的 `analysis` 新增必填 `capabilityStatus`（`complete/unavailable/failed`）。docs/07 camera-motion 示例暂未含该字段，schema 以决议为准。  
理由：无音轨、跨平台降级和失败必须显式可表达（失败可见不变量）。

### D-016：shots 相邻连续性的 partial 判定（M0）

决策：`shots.json` 自身无 status 字段；跨文件校验器以 `media.source.analysisCoverage` 是否含 `partial` 条目判定——含 partial 时"相邻镜头前 end == 后 start"降级为 warning，否则为 error。  
理由：docs/02 §4.2 要求区分正常完整分析与降级情况，契约未给 shots 自身状态字段。

### D-017：M1 stub 状态语义（M1）

决策：M1 阶段未实现的能力写显式降级产物而非省略文件：asr.json `status=skipped`（服务未配置）；music-flags.json `status=skipped` 且每镜头 state=unknown；audio-cuts.json `status=unavailable`（本构建无此能力，首末镜头仍输出 sourceStart/sourceEnd）；camera-motion.json `analysis.capabilityStatus=unavailable`；unified-media.json `status=skipped`、clips[] 填真实 clip 信息、shotStatuses 全 pending、terminal=false。  
理由：失败可见不变量（docs/00 §4.7）；降级矩阵中这些能力均为非致命。

### D-018：切镜相似度公式与入选路径实证（M1，CALIBRATION）

决策：histogramSimilarity=直方图相交 sum(min)；edgeSimilarity=`1-min(1,|eA-eB|/max(eA,eB,1e-6))`；adaptiveOutlier 增加最小绝对偏离 0.5 防 MAD≈0 噪声；边界置信度 high=score<-2、medium=score<0、low=其余。  
实证：纯色→纯色硬切两帧 edge_density 均为 0，score≈+0.87>0，**只能走 adaptiveOutlier**；纯绿与纯红灰度亮度接近（≈0.295 vs 0.298），灰度直方图法无法区分——属检测器已知局限，e2e 合成视频因此采用红/白/蓝。分析参数 analysisFps=2.0 + minimumFrames=8 意味着最小镜头约 4s，快切合成样片检不出属预期。

### D-019：pipeline 状态文件与 force 语义（M1）

决策：clips 不是独立契约 artifact，其复用状态记录在 output-dir 根的 `.memoloupe-pipeline.json`（实现元数据，与 manifest 同类）；`--force STEP` 只跳过该步骤的复用判定，下游指纹未变则仍 reused；render/validate 仅在"本次无任何产物步骤执行且状态指纹匹配"时 reused。  
理由：clips[] 嵌在 unified-media.json 中，无独立 manifest 条目可挂；force 语义保持最小惊讶。

### D-020：音频切点六特征定义（M2，CALIBRATION）

决策：rmsDb=20·log10(rms/32768)（地板 -99）；roughness=帧内 4 子窗 RMS 包络变异系数；amplitudeShape=峰均比；autocorrelation1ms/4ms=16/64 采样滞后归一化自相关（@16kHz）；novelty 局部尺度取 ±1s 窗 |Δ| 中位数与 MINIMUM_SCALES 的 max；峰值阈值 8.0 + 局部最大 + 关联窗一半最小间隔。  
理由：docs/03 §2.4 明确六特征精确定义为 CALIBRATION；实测 440→880Hz 切换 novelty>50 远超阈值。

### D-021：BGM 检测参数（M2，CALIBRATION）

决策：STFT Hann 窗 2048/hop 512 @22050Hz；bassEnergy=归一化采样（1/32768）低频带（≤250Hz）幅值和；texture event 阈值 |Δflatness|≥0.1、250ms 抑制；镜头裁定重叠比例 ≥0.5；ASR 不可用时降级为全片纹理分析且 confidence 降一档。  
理由：与 docs/07 示例阈值（musicBassEnergy 150）兼容；降级矩阵要求 ASR 失败时 music 降级而非失败。

### D-022：Apple Vision helper 策略（M2）

决策：单文件 Swift helper（helpers/apple-vision/main.swift），stdin JSON/stdout JSON/stderr 日志；swiftc 编译产物按 源码+swiftc版本+实现版本 哈希缓存；非 macOS/编译失败/运行失败一律 capabilityStatus=unavailable。追踪请求为 macOS 14+ 有状态 API；helper 输出取逆矩阵使 shiftX/Y 表示画面内容位移（shiftX<0 ↔ pan_right，与 docs/07 示例一致）。Vision 幅值实测低估 2-3 倍，分类只依赖方向一致性/单调性/相对幅值。  
理由：docs/01 §7.4 协议；D-005 认识论区分。

### D-023：模型服务适配与编排（M2）

决策：OpenAI-compatible 适配器（urllib，不加 httpx）；编排器重试语义为"首发 + 3 次重试"指数退避；PermanentServiceError 不重试直接回退单镜头；最终 batches[] 输出三组合并视图（部分失败拆成单镜头记录，failed 批次无 response 不伪造）；fallback 模型切换当前只在服务层记录 fallbackAttempted，真正换模型重发需服务层支持（待办）。  
理由：docs/03 §2.12 重试与合并规则；服务端口职责划分（docs/01 §7.2）。

### D-024：校验器 partial 修复与遗留（M2）

决策：cross_artifact 校验器已修复——failed 且无 response 的批次不再报"集合不一致"（partial 允许成败并存）。  
遗留：resolver 层的 needsReview 冲突理由（`build_observations_with_review`）尚未接入渲染层镜头列（render 改动留给 M3）；aligned shots 以派生指纹 `alignedWith` 重写，detect_shots 复用判定同时接受 base/aligned 指纹。

## 3. 推荐技术默认值

以下不是稳定产品契约，开发可调整，但要更新测试和本文件：

- Python 3.12。
- `uv` 管理项目和锁文件。
- macOS 14+ 作为 Apple Vision 完整能力环境。
- ffmpeg/ffprobe 从 PATH 探测，并记录版本。
- NumPy 用于音频和基础矩阵统计。
- HTML 使用原生 JS/CSS，不引入大型前端构建链。
- 测试使用 pytest。
- JSON Schema 使用 Draft 2020-12。
- 模型接口先提供 Mock 和 OpenAI-compatible 风格适配器，再接具体供应商。
- JSON 2 空格缩进、UTF-8、末尾换行。

## 4. 待校准算法

以下参数结构已确定，但真实值需要样例视频或原始设计进一步校准：

### A-001 视觉切镜

- histogram/edge similarity 精确公式；
- adaptive outlier 的窗口和 MAD 系数；
- 分析 fps；
- 候选去重和极短镜头合并。

已知固定常量应保留为默认起点，不代表完整算法已还原。

### A-002 音频切点

- roughness、amplitudeShape、autocorrelation 的精确定义；
- 局部归一化窗口；
- 峰值去重距离；
- 是否默认移动 final 视觉边界。

### A-003 BGM

- STFT window/hop；
- bass energy 单位；
- gap anchor 扩展规则；
- music/silent overlap 分类阈值。

### A-004 质量

- blurdetect 值方向和跨 ffmpeg 版本稳定性；
- 黑场/冻结最短持续时间；
- 多采样结果的聚合规则。

### A-005 Apple Vision

- frame shift、scale、rotation 的分类阈值；
- handheld/discontinuity 区分；
- preferred transform 后的运动方向。

### A-006 故事聚块

- gapMs 默认 1200 是否适合全部内容类型；
- 无 ASR 视频的视觉候选块策略；
- 极短 block 合并。

### A-007 Profile 统计

- 风格分布按镜头数还是时长加权；当前默认镜头数。
- hosted coverage 的确定规则。
- audio boundary aligned 的 slot 聚合阈值。

## 5. 尚缺但不阻塞实现的信息

- 完整 `rules/vocabulary.json`。
- 实际 ASR 服务和供应商扩展字段。
- UnifiedMLLM 端点、鉴权和最终 prompt。
- 文本模型端点。
- HTML 的视觉品牌和精确交互原型。
- 真实视频黄金样例。
- 性能目标和支持的最大视频时长。
- 分发、签名或安装方式。

处理策略：先用小词表、Mock 服务、离线模板和合成媒体实现；所有未知点必须处于适配器或配置边界。

## 6. 已识别的契约歧义

### C-001 `shots.json` 与 `shot-candidates.json`

数据协议称稳定名为 shots.json、现实现名为 raw/shot-candidates.json；早期架构又把两者描述为候选与对齐后的两个文件。当前设计采用单一稳定 shots artifact，内部同时包含 boundaries 和 detected/final shots；旧名仅兼容。

### C-002 evidenceRefs HTML/JSON 表示

JSON 使用数组；HTML data attribute 使用空格分隔。这是序列化边界，不视为冲突。

### C-003 story boundary source

原架构强调 ASR 确定性聚块，数据契约允许 model。当前默认 ASR，模型作为显式可选模式。

### C-004 music-flags status

字段表只列 complete，但系统必须表达无音轨、ASR 不可用和失败。实现会扩展或通过 manifest/coverage 表达 unavailable/failed；在冻结 schema 前应确认最终枚举。

### C-005 camera-motion unavailable

协议顶层未定义 status。跨平台降级需要状态。首版可通过 `analysis.capabilityStatus` 扩展或 manifest 表达，最终 schema 需统一。

### C-006 sourcePath 绝对路径

协议要求绝对路径，但分享产物存在隐私和不可移植问题。运行产物保留绝对路径；导出模式可生成脱敏副本，但不得冒充原运行产物。

### C-007 batch ID 与 block ID

两者都可为 B0001。它们属于文件局部命名空间，代码使用类型区分。若未来统一全局证据 URI，可把模型 batch 显示为 `MB0001`，但不能擅自改变稳定协议。

## 7. 变更流程

任何 AI 若要改变 MUST 级约束，应提交同一变更中的：

1. 设计决策说明；
2. schema 版本影响；
3. 数据迁移方案；
4. 缓存失效影响；
5. 测试更新；
6. 下游兼容说明。

仅调整 CALIBRATION 参数时无需提升契约版本，但必须：

- 更新配置默认值；
- 更新算法版本或指纹；
- 添加校准证据；
- 确保旧缓存失效。

## 8. 给后续开发 AI 的决策原则

遇到文档未覆盖的问题时：

1. 先保护证据和稳定 JSON。
2. 选择失败可见而不是猜测成功。
3. 选择可配置而不是硬编码。
4. 选择显式适配器而不是把供应商格式扩散到业务层。
5. 选择纯函数和小模块而不是大编排脚本。
6. 选择保留 raw 与 correction，而不是覆盖历史。
7. 无法确定语义时输出 unknown，不自行创造确定结论。
