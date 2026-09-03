<p align="center">
  <img src="./assets/readme/hero.zh-CN.svg" width="100%" alt="MemoLoupe — 一条命令，把参考视频拆成镜头、故事、风格三层可复刻档案">
</p>

<p align="center">
  <a href="README.md">English</a> | 中文
</p>

MemoLoupe 是一个**拉片分析工具**：给它一段参考视频，它把视频拆解成三层结构化档案——镜头、故事、风格——供你校对后用于复刻同类内容的创作。

## 你会得到什么

| 命令 | 产物 | 内容 |
|---|---|---|
| `memoloupe shot` | `shot-analysis.html` | 统一审片工作台：镜头与故事双轨时间线，每个镜头的内容、运镜、光线、声音，逐镜可回看证据 |
| `memoloupe story` | `story-analysis.html` | 故事结构：故事块、叙事插槽、块之间的关系；同时并入 shot 工作台的双轨时间线，人工复查在统一工作台完成 |
| `memoloupe profile` | `style-profile.json` | 风格档案：叙事/节奏/风格的分布与复刻要点（机器可读契约） |

所有结论都能在 HTML 里点回原始证据（clip、帧、音频段），模型拿不准的地方会明确标记，不会伪装成确定结论。

## 快速开始

环境要求：Python 3.12+、[uv](https://docs.astral.sh/uv/)、ffmpeg。macOS（Apple Silicon）可额外启用本地 ASR 与 Apple Vision 运镜分析。

```bash
# 1. 安装
uv sync
uv sync --extra asr-local   # 可选：本地语音识别（FireRedVAD + MLX Whisper）

# 2. 连接模型服务（交互式；API key 存系统 Keychain，绝不写入明文文件）
uv run memoloupe connect add qwen     # 或：connect add mimo
uv run memoloupe connect status       # 查看连接；connect test 做健康检查

# 3. 跑三阶段（管道自动使用当前 provider；未配置时对应步骤显式降级，
#    确定性分析不受影响）
uv run memoloupe shot    ./video.mp4 --output-dir ./out
uv run memoloupe story   --output-dir ./out
uv run memoloupe profile --output-dir ./out

# 4. 校验产物（schema + 跨文件一致性 + HTML 语义）
uv run memoloupe validate ./out --strict

# 5. 查看与校对
open ./out/shot-analysis.html                    # 或直接用浏览器打开
uv run memoloupe review --output-dir ./out       # localhost 人工校对界面
```

不想用 `connect`？传统环境变量配置仍然可用（无 active provider 时生效）：

```bash
cp .env.example .env   # 填入 MEMOLOUPE_TEXTMODEL__* / MEMOLOUPE_UNIFIEDMODEL__*
uv run memoloupe shot ./video.mp4 --output-dir ./out --env-file .env
```

## 它是怎么工作的

1. **确定性检测先行**——切镜、BGM、音频切点、质量、运镜由检测器实测（Apple Vision、ffmpeg），模型无法覆盖这些证据；
2. **模型只做语义**——多模态模型（MiMo 或任意 OpenAI 兼容端点）分析镜头内容/声音/剪辑功能；ASR 可完全本地运行（FireRedVAD 人声分离 + MLX Whisper）；文本模型只做 story 与 profile 的**文本**蒸馏，绝不接收视频或帧；
3. **受控词表 + 五态取值**——语义字段归一化到固定词表，每个值区分"实测没有 / 模型称没有 / 无法确认"；
4. **人工校对闭环**——story 结果与镜头合并为同一工作台的双轨时间线，校对结果以 overlay 形式叠加，重跑分析不会丢失。

## 产品边界

输出止于上面三个文件。Story Spine 生成、素材匹配、自动粗剪、FCPXML 导出**不属于本仓库**，它们是下游消费者。

## 文档与开发

- 设计与契约文档：[`docs/`](docs/)（从 `00_REPRODUCTION_SPEC.md` 开始）
- 贡献者/AI 协作者指南：[`AGENTS.md`](AGENTS.md)
- 测试：`uv run pytest`（单元 + 契约 + 集成 + e2e）
- 真实服务联调测试：`MEMOLOUPE_RUN_REAL_SERVICE_TESTS=1` 显式启用（默认不进 CI）
