# 测试策略与验收规范

## 1. 测试原则

测试不只验证函数输出，还要验证契约、不变量、可恢复性和降级行为。未知算法阈值不应阻止工程测试；对待校准算法优先测试方向性和结构性性质。

所有 bug 修复必须先添加可复现测试。所有契约变更必须同步更新 schema、夹具和迁移测试。

## 2. 测试分层

### 2.1 单元测试

覆盖纯函数：

- 时间换算与区间交集；
- ID 格式化与解析；
- JSON 指针/证据引用解析；
- vocabulary normalize；
- 五态转换；
- Observation resolver；
- 镜头边界构造与对齐；
- 音频特征和峰值选择；
- profile 聚合与分布；
- story block 聚块；
- fingerprint 构造。

### 2.2 契约测试

每个 JSON 文件至少包含：

- 最小合法 fixture；
- 完整合法 fixture；
- 缺必填字段；
- 错误类型；
- 非法枚举；
- 非法 ID；
- 越界时间；
- unknown/null/字段缺失区别。

契约文档中的示例应被提取或手工镜像为 fixtures，并在 CI 中验证。

### 2.3 跨文件测试

至少覆盖：

- 缺失 shot 引用；
- 重复 shot ID；
- source revision 不一致；
- 镜头区间重叠/断裂；
- clip 集合与 shotStatuses 不一致；
- story block 引用未知 shot；
- slot 引用未知 block；
- profile slot 与 story slot 不一致；
- evidence ref 文件或数组索引不存在。

### 2.4 工具集成测试

使用小型合成媒体验证 ffprobe/ffmpeg：

- 单色视频，无音轨；
- 两段明显不同颜色的硬切视频；
- 黑场夹在两段画面之间；
- 音频在画面切点同步突变；
- 画面切但音频连续；
- 静音、低、中、高、削波音频；
- 极短视频；
- 不同帧率和旋转 metadata。

合成媒体可以由测试脚本在临时目录生成，不进入仓库大文件。

### 2.5 服务适配器测试

真实网络服务不进入默认测试。使用可录制或程序化 mock 覆盖：

- 成功结构化 JSON；
- JSON fence；
- 非法 JSON；
- 漏 shot；
- 重复 shot；
- 未知 shot；
- 模型输出“无”；
- 429/500/timeout；
- batch 失败后 single fallback；
- fallback model；
- checkpoint 恢复。

### 2.6 HTML 测试

- snapshot 只用于稳定模板片段，不把整页微小空白锁死。
- HTML parser 校验结构和属性。
- 浏览器 smoke test 验证播放器定位、编辑、导出 correction、过滤和确认。
- 模型文本包含 `<script>`、引号、换行和中文时必须安全显示。

### 2.7 端到端测试

至少三个场景：

1. 无外部模型、无 Apple Vision 的纯确定性 Phase 1。
2. Mock ASR/MLLM 的完整三阶段。
3. 中途故障后重跑，只补失败镜头。

## 3. 属性测试与不变量

推荐使用生成式/属性测试验证：

- 任意合法 shot 列表排序后不重叠。
- 对齐算法不会产生负时长。
- duration 始终等于 end-start。
- 任何模型“无”都不会产生 absent。
- normalize 要么返回合法词表值，要么 unmapped/unknown，不返回半合法值。
- 对任意合法 block/slot，所有引用闭合。
- 分布非负且总和在容差内。
- 原子写入故障不会破坏已有完整文件。

## 4. 黄金样例策略

在没有原项目真实输出时，建立两类黄金样例：

### 4.1 合成黄金样例

已知画面和音频事件，适合验证确定性算法和时间映射。例如：0-2 秒红色、2-4 秒蓝色，2 秒音频同时换频率。

### 4.2 人工校准样例

未来提供 2～5 支真实短视频后，人工记录：

- 期望镜头边界容差；
- BGM 区间；
- 明显质量问题；
- 典型运镜候选；
- 可接受的故事块。

真实样例不要求每个模型描述逐字一致，而验证受控值、状态和结构是否合理。

## 5. Phase 1 验收

### 5.1 必须通过

- ffprobe 能生成完整 media.json。
- 两段硬切合成视频检测到唯一主要边界，误差在一分析帧内。
- shots final 区间覆盖分析范围、连续、无重叠。
- 无音轨时生成显式 unavailable 音频产物。
- clip 与帧证据均按 shotID 命名并可打开。
- 模型全失败时仍生成 shot HTML。
- 每个单元格五态合法。
- 模型“无”回归测试通过。
- JSON、跨文件、HTML 校验全部通过。

### 5.2 允许待校准

- 真实视频切镜召回率/精确率。
- BGM 检测阈值。
- Apple Vision 运镜分类阈值。
- 质量检测阈值。
- 模型 prompt 的描述质量。

待校准项不得破坏 schema、状态和证据。

## 6. Phase 2 验收

- ASR gap 在边界值前后行为明确并有测试。
- 首镜头必定进入首 block。
- 无 ASR 时生成合法 scaffold。
- 默认所有 shot 恰好属于一个连续 block。
- 模型不能创建或删除 shot。
- block 关系引用闭合。
- story HTML 不含视频模型请求能力。
- story JSON 和 HTML 通过严格校验。

## 7. Phase 3 验收

- 无模型也能生成 schemaVersion 2 的 style profile。
- slot range、share、shotCount、avg duration 可从 raw 重新计算一致。
- 分布和 coverage 计算规则固定且有测试。
- 模型不能覆盖确定性统计。
- distillStatus 与模型执行状态一致。
- hook/payoff 缺失时为 null，不伪造。
- discussion item 结构完整且 defaultIfUnanswered 存在。

## 8. 恢复与缓存验收

- 同配置重跑命中缓存。
- 改 HTML 模板只重渲染 HTML。
- 改一个模型 group prompt 只失效对应 group 及下游。
- 改 final 边界使所有依赖 clip/区间的产物失效。
- 删除缓存引用文件后不得误报 reusable。
- batch 完成一半时强制中断，重跑只请求未完成 shot。
- 并发启动两个写 pipeline 时第二个安全失败，不损坏输出。

## 9. 性能基线

首版不设绝对速度承诺，但必须采集：

- 每个步骤耗时；
- ffmpeg 峰值并发；
- 模型请求次数、重试次数和字节量；
- clip/帧/JSON 磁盘占用；
- 缓存重跑节省的步骤。

性能优化不得通过跳过 schema 校验、证据写入或 checkpoint 实现。

## 10. 发布门槛

一个阶段可标记实现完成，必须同时满足：

1. 对应 schema 已提交。
2. 最小与完整 fixtures 已提交。
3. 单元、契约、集成测试通过。
4. 降级路径已测试。
5. CLI help 和错误退出码可用。
6. 生成样例通过 strict validate。
7. 文档中的 CALIBRATION 项未被误标为已验证事实。

## 11. 推荐实施里程碑

### M0：可执行契约

- 项目骨架、配置、数据类、JSON Schema、ArtifactStore、校验器、fixtures。

### M1：确定性 Phase 1

- probe、shots、frame、clip、audio energy、quality、降级 HTML。

### M2：音频与智能分析

- audio cuts、ASR、music、MLLM、Apple Vision、Observation resolver。

### M3：渲染收尾与故事分析

- Phase 1 review reason 呈现、ASR 聚块、文本模型、story HTML、CLI 与严格校验。

### M4：风格档案

- 确定性聚合、模型蒸馏、profile CLI、schema v2 与跨文件校验。

### M5：真实服务、词表与校准

- 真实服务联调、完整词表、黄金视频参数校准、HTML 人工校对体验和发布准备。

M3–M5 的当前 plan 拆分与执行状态见 `docs/08_DEVELOPMENT_ROADMAP.md`。

每个里程碑都必须形成可运行纵向产物，不接受只提交大量空模块。
