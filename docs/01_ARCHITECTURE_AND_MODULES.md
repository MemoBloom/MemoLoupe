# 系统架构与模块设计

## 1. 架构目标

架构必须同时满足四件事：

1. 确定性检测与模型推断隔离。
2. raw 证据、Observation 和呈现层分离。
3. 每个昂贵步骤可缓存、可重试、可独立测试。
4. 后续可以替换 ASR、MLLM、Apple Vision 或前端，而不修改稳定数据契约。

## 2. 分层与依赖方向

```text
CLI 入口层
  └─ 阶段编排层
      ├─ 确定性媒体检测层
      ├─ 外部能力适配层
      ├─ 故事/档案计算层
      └─ Observation 解析层
          ├─ 契约与词表层
          └─ Artifact/基础设施层

呈现与校验层只读取稳定契约和 Observation，不直接调用检测器或模型。
```

依赖只能向下。尤其禁止：

- 检测器导入 HTML 渲染代码；
- 模型客户端直接写最终 HTML；
- 模板解析器直接调用 ffmpeg；
- 下游 profile 聚合绕过稳定 JSON 读取临时内存对象；
- 任意模块自行拼接 raw 路径。

## 3. 推荐仓库结构

```text
MemoLoupe/
├── pyproject.toml
├── README.md
├── AGENTS.md
├── src/memoloupe/
│   ├── cli/
│   │   ├── main.py
│   │   ├── shot_analysis.py
│   │   ├── story_analysis.py   # 实现模块，由 shot --story-only / 链式流程调用
│   │   └── profile_build.py
│   ├── core/
│   │   ├── config.py
│   │   ├── errors.py
│   │   ├── logging.py
│   │   ├── time_ranges.py
│   │   ├── hashing.py
│   │   └── atomic_io.py
│   ├── artifacts/
│   │   ├── store.py
│   │   ├── manifest.py
│   │   ├── schemas.py
│   │   └── migrations.py
│   ├── media/
│   │   ├── proc.py
│   │   ├── concurrency.py
│   │   ├── probe.py
│   │   ├── proxy.py
│   │   ├── frames.py
│   │   ├── shots.py
│   │   ├── audio_cuts.py
│   │   ├── audio_music.py
│   │   ├── audio_energy.py
│   │   └── quality.py
│   ├── vision/
│   │   ├── protocol.py
│   │   ├── unavailable.py
│   │   └── apple_vision.py
│   ├── services/
│   │   ├── base.py
│   │   ├── asr.py
│   │   ├── unified_media.py
│   │   ├── text_model.py
│   │   └── mock.py
│   ├── analysis/
│   │   ├── vocabulary.py
│   │   ├── observations.py
│   │   ├── media_groups.py
│   │   ├── media_orchestrator.py
│   │   ├── story_blocks.py
│   │   └── profile_aggregate.py
│   ├── render/
│   │   ├── shot_html.py
│   │   └── corrections.py
│   └── validate/
│       ├── json_contracts.py
│       ├── cross_artifact.py
│       └── html_contract.py
├── schemas/
├── rules/
│   ├── vocabulary.json
│   └── completion.json
├── templates/
│   ├── shot-analysis.html
│   └── assets/
├── helpers/apple-vision/
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   ├── e2e/
│   └── fixtures/
└── docs/
```

为了兼容原说明，可以提供根级 `run_shot_analysis.py`、`run_profile_build.py` 薄包装，但业务实现必须位于包内。

## 4. 核心基础设施

### 4.1 `core.config`

职责：

- 合并 CLI、配置文件和环境变量；
- 类型校验；
- 输出最终有效配置的脱敏快照；
- 生成分阶段配置指纹。

推荐优先级：CLI > 环境变量 > 项目配置文件 > 内置默认值。

配置至少分组：

- `runtime`
- `ffmpeg`
- `shots`
- `audioCuts`
- `music`
- `quality`
- `vision`
- `asr`
- `unifiedModel`
- `story`
- `profile`
- `render`

密钥不得进入配置快照和指纹。

### 4.2 `core.atomic_io`

公开接口建议：

```python
def read_json(path: Path) -> dict[str, object]: ...
def write_json_atomic(path: Path, value: object) -> None: ...
def write_text_atomic(path: Path, text: str) -> None: ...
```

写入流程：同目录临时文件、flush、可选 fsync、`os.replace`。写入前在内存中完成 schema 校验；写入后可选重新读取验证。

### 4.3 `media.proc`

所有外部进程必须通过统一封装执行。

```python
@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    elapsed_sec: float

def run_process(
    argv: Sequence[str],
    *,
    timeout_sec: float,
    stdin: bytes | None = None,
    capture_limit_bytes: int | None = None,
) -> ProcessResult: ...
```

要求：

- 使用 argv 数组，不通过 shell 字符串执行。
- 超时后终止整个进程组。
- 对 stderr 截断，防止日志和内存失控。
- 错误对象保留脱敏命令、退出码和 stderr 尾部。
- 区分 probe、scan、clip、frame 四类超时。

### 4.4 `media.concurrency`

至少提供：

- 全局 ffmpeg 信号量；
- 模型请求信号量；
- 后台 nice 策略；
- 可取消任务上下文。

信号量应在真正启动子进程前获取，并在异常路径可靠释放。

## 5. ArtifactStore 与 Manifest

### 5.1 目标

ArtifactStore 是所有产物路径、状态和指纹的唯一入口。业务模块不得硬编码 `raw/foo.json`。

```python
class ArtifactName(StrEnum):
    MEDIA = "media"
    SHOTS = "shots"
    AUDIO_CUTS = "audio-cuts"
    FRAME_EVIDENCE = "frame-evidence"
    ASR = "asr"
    MUSIC_FLAGS = "music-flags"
    UNIFIED_MEDIA = "unified-media"
    CAMERA_MOTION = "camera-motion"
    QUALITY_FLAGS = "quality-flags"
    AUDIO_ENERGY = "audio-energy"
    STORY_BLOCKS = "story-blocks"
    STYLE_PROFILE = "style-profile"

class ArtifactStore:
    def path(self, name: ArtifactName) -> Path: ...
    def read(self, name: ArtifactName) -> dict[str, object]: ...
    def write(self, name: ArtifactName, data: object, metadata: WriteMetadata) -> None: ...
    def is_reusable(self, name: ArtifactName, fingerprint: str) -> bool: ...
```

### 5.2 Manifest

`manifest.json` 是推荐新增的实现元数据，不改变业务契约。建议记录：

```json
{
  "manifestVersion": 1,
  "sourceRevisionID": "a1b2c3d4e5f6",
  "artifacts": {
    "shots": {
      "path": "raw/shots.json",
      "legacyPaths": ["raw/shot-candidates.json"],
      "schemaVersion": "1.0",
      "fingerprint": "...",
      "status": "complete",
      "updatedAt": "..."
    }
  }
}
```

manifest 不得成为唯一真相。业务状态仍以各文件内部状态和 schema 为准。

## 6. 契约模型层

推荐使用标准 Python 数据类加显式解析器，或选择成熟验证库。无论技术选择如何，必须满足：

- 读取时拒绝错误类型和非法枚举；
- 能区分字段不存在与 `null`；
- 写出稳定 JSON；
- 未知扩展字段可按文件策略保留，但不得静默影响核心语义；
- 错误信息带 JSON 路径；
- schema 能从同一来源生成或与数据类做一致性测试。

为避免字符串 ID 混用，应定义 `ShotID`、`AudioBoundaryID`、`FrameEvidenceID`、`StoryBlockID`、`StorySlotID` 等轻量类型或构造/验证函数。

## 7. 外部能力端口

### 7.1 ASR

```python
class ASRService(Protocol):
    def transcribe(self, media_path: Path, request: ASRRequest) -> ASRResult: ...
```

适配器必须归一为稳定 `asr.json`，不得把供应商响应直接泄漏为主契约。供应商扩展可放在命名空间字段或调试文件中。

### 7.2 UnifiedMLLM

```python
class UnifiedMediaService(Protocol):
    def analyze_batch(self, clips: Sequence[ModelClip], group: AnalysisGroup) -> GroupResponse: ...
```

服务端口负责 HTTP、鉴权、超时、JSON 文本提取；编排器负责分组、批次、重试、checkpoint 和结果覆盖检查。

### 7.3 文本模型

```python
class TextModelService(Protocol):
    def analyze_story(self, request: StoryRequest) -> StoryResponse: ...
    def distill_profile(self, request: ProfileDistillRequest) -> ProfileDistillResponse: ...
```

故事和档案请求不得复用不受约束的聊天接口返回自由文本；必须要求结构化 JSON 并做 schema 校验。

### 7.4 Apple Vision

Python 通过一个明确协议调用 macOS helper。推荐 helper 从 stdin 接收 JSON 请求、stdout 只输出 JSON，日志写 stderr。

```json
{
  "source": "/path/video.mp4",
  "shots": [{"shotID": "SH0001", "startMs": 0, "endMs": 3203}],
  "sampleFps": 2.0,
  "maximumFramesPerShot": 12
}
```

helper 不可用时适配器返回能力 unavailable，不抛出导致整个 Phase 1 终止的致命异常。

## 8. Observation 解析层

`analysis.observations` 是业务语义核心。它从多个 raw 文件读取证据，按字段策略构造 Observation。

```python
@dataclass(frozen=True)
class Observation:
    field: str
    shot_id: str
    value: object | None
    state: ValueState
    confidence: Confidence
    evidence_refs: tuple[str, ...]
    source: str
    verified: bool
    original_value: object | None = None
```

每个字段使用显式 resolver，不采用一个巨大的条件函数：

```python
class FieldResolver(Protocol):
    field_name: str
    def resolve(self, context: ShotEvidenceContext) -> Observation: ...
```

解析优先级因字段而异，例如：

- speech：人工修正 > ASR > 模型 > unknown。
- BGM 是否存在：确定性 music flags；模型只提供 style。
- camera movement：Apple Vision 是运动证据，模型提供语义标签；冲突时标记 review，不静默覆盖。
- quality flags：确定性检测器。
- framing、composition、tone：模型，经词表归一化。

## 9. 编排层

每个阶段编排器应是可注入依赖的普通 Python 对象，不把所有逻辑写进 CLI。

```python
class ShotAnalysisPipeline:
    def run(self, request: ShotAnalysisRequest) -> PipelineReport: ...

class StoryAnalysisPipeline:
    def run(self, request: StoryAnalysisRequest) -> PipelineReport: ...

class ProfileBuildPipeline:
    def run(self, request: ProfileBuildRequest) -> PipelineReport: ...
```

`PipelineReport` 至少包含：阶段状态、已执行步骤、复用步骤、警告、失败、产物路径和耗时。

## 10. CLI 契约

推荐一个主命令和两个子命令：

```text
memoloupe shot INPUT --output-dir DIR [options]  # 默认镜头+故事合并流程；--skip-story 只跑镜头；--story-only 只跑故事（承接原 story 命令全部参数）
memoloupe profile --output-dir DIR [options]
memoloupe validate TARGET [--strict]
```

兼容包装：

```text
python run_shot_analysis.py INPUT --output-dir DIR
python run_profile_build.py --output-dir DIR
```

通用退出码建议：

- `0`：完成，允许非致命 warning。
- `2`：用户参数或配置错误。
- `3`：输入/契约错误。
- `4`：必要外部工具不可用。
- `5`：阶段执行失败。
- `6`：校验失败。

CLI 默认输出人类可读摘要；`--json-report` 输出机器报告。错误写 stderr。

## 11. 日志与诊断

每条日志建议包含：

- `runID`
- `phase`
- `step`
- `shotID` 或 `batchID`
- `elapsedMs`
- `status`

日志分级：

- INFO：步骤开始、完成、缓存命中。
- WARNING：降级、低置信度、部分失败、待人工复核。
- ERROR：当前步骤失败。
- DEBUG：脱敏命令、指纹组成、响应解析路径。

禁止在日志中输出二进制内容、Data URI、完整模型返回或凭据。
