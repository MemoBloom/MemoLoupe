# HTML 校对界面与校验设计

## 1. 定位

`shot-analysis.html` 和 `story-analysis.html` 是可离线打开的人工校对视图。它们不是机器下游的主输入，但必须携带足够机器属性，以便：

- 校验展示是否忠实于 JSON；
- 保存人工修正；
- 判断文档状态；
- 从 UI 单元格追溯到 raw 证据。

HTML 不得包含模型调用、媒体分析或业务聚合逻辑。渲染器先生成 Observation，再把 Observation 映射到模板。

## 2. 文档元数据

根节点或指定 metadata 节点必须表达：

- document type：`shotAnalysis` 或 `storyAnalysis`
- status：`draft`、`underReview`、`confirmed`、`outdated`
- contract version
- source revision ID
- generated time
- output directory 相对基准

推荐：

```html
<html
  data-document-type="shotAnalysis"
  data-document-status="draft"
  data-contract-version="1.0"
  data-source-revision="a1b2c3d4e5f6">
```

文档 status 规则：

- `draft`：机器生成，尚未开始人工核实。
- `underReview`：至少一项人工修改或核实，但未满足 completion 规则。
- `confirmed`：满足 `rules/completion.json`，并由用户显式确认。
- `outdated`：源 revision、关键上游 fingerprint 或契约版本变化。

确认操作必须显式，不得仅因所有 checkbox 被选中自动改 confirmed。

## 3. Shot Analysis 结构

### 3.1 页面区域

建议包含：

1. 顶部元数据与状态栏。
2. 视频播放器。
3. 镜头导航和过滤器。
4. 横向镜头表格：每列一个镜头、每行一个字段。
5. 证据抽屉或详情面板。
6. 人工修改和确认操作区。
7. 校验错误/警告区。

### 3.2 镜头列

每列至少展示：

- shotID；
- final 时间码和时长；
- 代表帧；
- needsReview；
- 播放按钮；
- 镜头级验证进度。

镜头列头的机器可读 review 语义（稳定契约）：

```html
<th scope="col" data-shot-id="SH0001" data-start-ms="0" data-end-ms="3203"
    data-needs-review="true"
    data-review-reasons="[&quot;visual.cameraMovement：…不一致，需人工复核&quot;]">
```

- `data-review-reasons` 为 JSON 字符串数组：resolver 冲突理由在前，`shots.json`
  `needsReview=true` 时追加 `"shots.json 标记 needsReview"`；无理由时为 `[]`。
- 不变量：`data-needs-review="true"` 当且仅当 `data-review-reasons` 非空。
- `title` 属性仅为人类可读 tooltip，机器消费一律走 `data-review-reasons`。

点击镜头标题、缩略图或播放按钮时，播放器定位到 finalStartMs，并只播放到 finalEndMs。必须避免播放器越界继续播放下一镜头；允许用户关闭区间循环。

### 3.3 字段单元格

字段单元格必须具有：

```html
<td
  data-field="visual.framing"
  data-shot-id="SH0001"
  data-value-state="value"
  data-confidence="medium"
  data-evidence-refs="raw/unified-media.json#..."
  data-source="unifiedModel"
  data-verified="false">
```

纯标签单元格允许 `data-value-state=labelOnly`。`labelOnly` 仅属于 HTML 表现契约，不属于 Observation 五态。

规则：

- attribute 值必须正确 HTML escape。
- evidence refs 在 HTML 中空格分隔；单个引用自身不得包含未编码空格。
- 可见文本必须与 data state/value 语义一致。
- unknown、unmapped、absent-claimed 必须有明显但不过度警示的不同视觉状态。
- absent 和 absent-claimed 不得显示成完全相同文案或颜色。
- confidence=unknown 不得隐藏。

## 4. Story Analysis 结构

页面至少包含：

- 视频播放器和镜头跳转；
- story block 顺序；
- block 覆盖镜头和时间；
- block 边界信号；
- 叙事字段；
- slot 聚合；
- block 关系引用；
- 人工校对与确认。

story block DOM 元素只能出现在 `storyAnalysis` 文档。shot 模板出现 story block 必须由校验器报错。

推荐属性：

```html
<section
  class="story-block"
  data-story-block-id="B0001"
  data-shot-ids="SH0001 SH0002"
  data-start-ms="0"
  data-end-ms="6400">
```

## 5. 人工修正

### 5.1 保存模型

HTML 可以内嵌 JS，但静态 HTML 无权直接覆盖本地文件。推荐支持两种模式：

1. 纯离线模式：导出 corrections JSON，由 CLI 导入。
2. 本地 review server 模式：通过 localhost API 原子写入 `corrections/`。

两种模式必须产生同一 correction schema。

### 5.2 修正操作

用户可以：

- 修改受控值；
- 将 unmapped 映射为合法值；
- 显式设置 unknown；
- 核实或取消核实；
- 调整 final 镜头边界；
- 调整 story block/slot 归属；
- 添加备注。

用户不应直接编辑 raw evidence。调整边界时：

- 保留 detected 边界；
- 更新 correction overlay；
- 校验相邻镜头连续性；
- 标记所有依赖 final 边界的下游产物 outdated。

### 5.3 防止丢失

- 页面离开前检测未导出更改。
- 保存返回明确成功/失败。
- correction 带 source revision。
- 重渲染自动应用兼容 correction。
- 新源 revision 不自动应用旧 correction。

## 6. HTML 资源策略

- 禁止 `<script src>` 外链脚本。
- 默认禁止外部 CDN、字体和网络图片。
- CSS/JS 可内联，或由渲染器嵌入本地 asset 内容。
- 视频和帧使用相对路径。
- 不把完整视频或帧 base64 内嵌进 HTML。
- Content Security Policy SHOULD 限制网络访问；若内联脚本需要例外，应由模板固定而非动态拼接用户内容。

## 7. JSON 校验层

### 7.1 单文件 schema

每次写 artifact 前执行 schema 校验。错误信息必须包含：

- 文件逻辑名；
- JSON 路径；
- 期望类型/枚举；
- 实际值摘要。

### 7.2 跨文件语义

写完阶段后执行 cross-artifact 校验，包括：

- source revision 一致；
- shot 覆盖完整；
- 时间区间合法；
- 引用 ID 存在；
- evidenceRefs 可解析；
- aggregate 数字一致；
- complete/partial/status 计数一致；
- profile slot 与 story slot 一致。

## 8. HTML 语义校验器

使用标准库 `html.parser.HTMLParser` 实现，避免校验依赖浏览器纠错行为。

### 8.1 结构检查

- document type 只能为 shotAnalysis/storyAnalysis。
- status 只能为 draft/underReview/confirmed/outdated。
- id 不得重复。
- 必需 metadata 存在。
- 表格必须有合法 table/tbody/tr/td 嵌套。
- 镜头列与行结构合法。
- story-block 不得出现在 shotAnalysis。
- shotAnalysis 必须包含至少一个 shot column。
- 解析错误要报告具体行列。

### 8.2 单元格检查

- 分析单元格具有 `data-value-state`。
- 合法值为五态加 `labelOnly`。
- 五态单元格必须有 confidence、source、verified。
- verified 只能是 `true`/`false`。
- evidence refs 格式正确。
- 每镜头至少有一个可追溯证据列。
- `unmapped` 应保留可见原始值或修正入口。
- 镜头列头必须携带 `data-needs-review ∈ {true,false}` 与
  `data-review-reasons`（JSON 字符串数组），且二者一致：
  needs-review="true" 当且仅当 reasons 非空。

### 8.3 安全检查

- 禁止外链 script。
- 禁止 `javascript:` URL。
- 用户/模型内容必须 escape，不能形成标签或属性。
- 不允许模板把 API key、授权头或 Data URI 输出到页面。

### 8.4 数据一致性

严格校验模式读取 raw JSON，验证：

- 页面 shotID 集合与 shots.json 一致。
- 时间显示和 data 属性与 final 边界一致。
- Observation state/value/source 与 resolver 结果一致，人工 correction 除外。
- evidence refs 指向正确 shot。
- 文档 source revision 一致。
- shots.json 标记 `needsReview=true` 的镜头，页面列头 `data-needs-review`
  必须为 `true`（resolver 冲突理由可独立置 true，反向不要求）。

### 8.5 回归护栏

必须针对以下历史风险建立专门检查：

- Unified model 响应数组与请求 shot ID 形状不一致。
- 因错误使用第一个数组元素而把两个 shot 合并。
- av boundary 在一处错位后污染整行。
- 缺 tbody/tr 时只报模糊 parse error。
- 引号、escape 错误导致 data 属性被截断。
- 用错 shot/story 模板。

## 9. Completion 规则

`rules/completion.json` 推荐声明：

```json
{
  "version": 1,
  "documents": {
    "shotAnalysis": {
      "requiredFields": ["visual.content", "visual.framing", "audio.speech"],
      "requireVerifiedStates": ["unmapped", "absent-claimed"],
      "allowUnknown": true,
      "requireValidEvidenceRefs": true
    },
    "storyAnalysis": {
      "requiredBlockFields": ["primaryRole", "coreContent", "audienceReaction"],
      "requireAllShotsAssigned": true,
      "requireAllBlocksAssignedToSlot": true
    }
  }
}
```

初版 completion 细节为推荐实现。无论规则如何，confirmed 必须是用户显式动作，且确认前必须通过严格校验。

## 10. 可访问性与易用性

- 所有交互可使用键盘。
- 播放、确认、编辑控件有可访问名称。
- 状态不能只靠颜色区分。
- 缩略图有描述性 alt。
- 表格行列标题使用正确 th/scope。
- 时间码可复制。
- 大量镜头时支持冻结字段列、横向滚动和按 needsReview 过滤。
- 页面打印不是核心目标，但不应完全不可读。
