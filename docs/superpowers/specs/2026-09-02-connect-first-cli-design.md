# Connect-First CLI 设计（memoloupe connect）

日期：2026-09-02
状态：已确认方向（CLI_CONNECT_TODO.md 为产品输入，本文档为工程设计）
分支：feat/connect-cli

## 背景与目标

当前 CLI 的配置完全面向开发者（env / `.env` 三组独立服务配置）。产品方向：
用户只需 `memoloupe connect add qwen`（或 `mimo`）连接自己的模型服务，
之后 `shot`/`story`/`profile` 自动使用 active provider，不需要理解
ASR / unifiedMedia / textModel 三个独立配置组。

目标 UX：

```bash
memoloupe connect add qwen
memoloupe shot video.mp4 --output-dir out
memoloupe story --output-dir out
memoloupe profile --output-dir out
```

## 明确不做（本期）

- `memoloupe login`（仅为未来官方托管服务保留概念）、账号、计费、web dashboard
- 纯本地体验、任意别名 provider、FCPXML / Story Spine / 自动粗剪
- 不改 shot/story/profile 的现有参数形态（`--out` 别名与 story/profile 位置参数
  属独立 UX 变更，不在本期；TODO 的目标 UX 示例用现有 `--output-dir` 表达）

## 设计

### 1. CLI 分发修复（TODO §1）

`cli/main.py::_dispatch` 现有前置分流覆盖 review/import-corrections/story/profile，
唯独 `shot` 走主 parser 的 `argparse.REMAINDER`，导致 `memoloupe shot --help`
被主 parser 的 help action 拦截、只显示浅层帮助。修复：把 `shot` 加入前置分流
（`if argv[:1] == ["shot"]: return run_shot_analysis(argv[1:])`），主 parser 中
shot 子 parser 条目保留（供顶层 `--help` 列表展示）。新增 CLI 分发回归测试。

### 2. 连接存储（TODO §3）— `connect/store.py`

- 路径：`~/.config/memoloupe/connections.json`；`ConnectionStore(path=...)` 显式注入，
  测试用 `tmp_path`，绝不写真 HOME；env `MEMOLOUPE_CONNECTIONS_PATH` 可覆盖默认路径。
- schema version 1（按 TODO 给定结构：`version/activeProvider/providers{...}`）。
- 写入用现有 `core/atomic_io.write_json_atomic`（原子替换）。
- 显式拒绝：坏 JSON、非 dict 顶层、`version != 1`、未知 providerId、
  activeProvider 不在 providers 中、必填字段缺失、capability 非布尔——抛
  `ConnectionStoreError`（含具体原因）。
- `connections.json` 不存 API key（只有 baseUrl/models/capabilities/时间戳）。

### 3. 凭据存储（TODO §4）— `connect/secrets.py`

- `SecretStore` Protocol：`get(provider_id) -> str | None`、`set(provider_id, secret)`、
  `delete(provider_id)`。
- `KeychainSecretStore`：macOS 通过 stdlib `subprocess` 调 `/usr/bin/security`
  （`add-generic-password -U` / `find-generic-password -w` / `delete-generic-password`），
  identity `service: memoloupe`、`account: provider:<id>`。**不引入 keyring 依赖**
  （延续项目纯标准库网络/系统调用的取向，D-042 精神）。
- `MemorySecretStore`：进程内 dict，供测试与 CI；`MEMOLOUPE_SECRET_STORE=memory`
  强制使用。
- 默认选择 `default_secret_store()`：环境变量强制 → macOS 且 `security` 可用 →
  Keychain；否则 MemorySecretStore + warning（凭据仅进程内有效）。
- 复用 `core.config.redacted_snapshot` 的键名规则做日志脱敏；`connect status`
  只显示"已保存/未保存"，永不显示 secret 本体。新增 `redact_secret(text, secret)`
  辅助（包装 `services/base.py::redact_text`）。

### 4. Provider 注册表（TODO §5）— `connect/registry.py`

```python
@dataclass(frozen=True)
class ProviderSpec:
    provider_id: str          # "qwen" | "mimo"
    label: str
    default_base_url: str
    default_media_model: str
    default_text_model: str
    default_asr_model: str | None
    capabilities: dict[str, bool]  # mediaUnderstanding/text/asr
    health_check_path: str    # 相对 baseUrl，GET 鉴权探测
```

- `qwen`：baseUrl `https://dashscope.aliyuncs.com/compatible-mode/v1`，
  media `qwen3.5-omni`、text `qwen-plus`（取自 TODO 示例），asr 不支持。
- `mimo`：baseUrl `https://api.xiaomimimo.com/v1`，media `mimo-2.5`、
  text `mimo-v2.5`（与 .env.example 一致），asr 字段保留（null/false）。
- 注册表是 provider 知识的唯一入口；shot/story/profile 不出现 provider 专属逻辑。
- health check：`GET {baseUrl}/models`，Bearer 鉴权，超时 15s；HTTP 2xx 为通过。
  网络层复用 `services/base.py` 的 urllib 封装与错误分类。

### 5. connect CLI（TODO §2/§6/§7）— `cli/connect.py`

`memoloupe connect <sub>`，经 `_dispatch` 前置分流：

- `add <qwen|mimo>`：交互式（getpwd 收 key，其余带默认值回车确认）；同时支持
  非交互参数 `--api-key-env VAR`（从环境变量读 key）、`--base-url`、`--media-model`、
  `--text-model`、`--asr-model`、`--no-active`。流程：保存 provider 配置 → 保存
  secret → health check → 通过则设为 active 并打印下一步命令；health check 失败
  时配置仍保存、警告、不设 active（退出码 0，warning 明示）。
- `status`：列出 providers、active 标记、capabilities、secret 是否已保存（脱敏）。
- `test [provider]`：对 active（或指定）provider 重跑 health check；失败退出码 5。
- `switch <provider>`：切换 activeProvider。
- `remove <provider>`：删除配置 + 删除 keychain secret；删的是 active 时清空
  activeProvider。
- `list`：简表输出。

退出码沿用 docs/01 §10：0 完成 / 2 参数错误 / 3 输入或配置契约错误 /
4 外部工具不可用 / 5 执行失败。

### 6. 管道路由（TODO §8）— `connect/runtime.py`

新增 `resolve_active_provider(config, *, store=None, secrets=None) -> tuple[dict, str]`：

- 返回 `(config, source)`，source ∈ `"provider" | "env" | "none"`。
- active provider 存在：把 provider 的 baseUrl/apiKey/models 展开叠加到
  `unifiedModel` 与 `textModel` 两个配置组（deep merge，provider 值优先），
  provider 支持 ASR 时同样叠加 `asr` 组。返回新 dict，不改原对象。
- 无 active provider：原样返回（现有 env 路径不变）。
- 接入点只改三个 CLI 模块（shot_analysis/story_analysis/profile_build）中
  `load_config()` 的调用处，替换为 `load_config()` + `resolve_active_provider()`；
  `--mock-services` / `--mock-text-model` / 显式注入服务的测试路径完全不变。
- 两者皆无（none）：**保持现有降级契约**（unified/text 产物 skipped），但把
  warning 文案改为可操作的 onboarding 提示（"运行 memoloupe connect add qwen
  连接模型服务，或配置 MEMOLOUPE_* 环境变量"）。不硬失败——无模型跑确定性
  分析是既有契约（README 承诺、D-042 降级矩阵），TODO §8 的 "fail" 按
  "清晰报错信息"落实为 warning 文案而非中断管道。

### 7. ASR 自动路由（TODO §9）

`asr.provider` 新增合法值 `"auto"`（在 `services/asr.py::build_asr_service` 内实现）：

1. 本地 ASR 可用（`import fireredvad` + `mlx_whisper` 探测成功）→
   `LocalFireRedVadMlxASR`；
2. 否则远程 ASR 三项（baseUrl/apiKey/model）齐全 → 按 transport 构造远程服务；
3. 否则返回 None（走现有显式降级，asr.json skipped/unavailable）+ 打 warning
   说明原因。不静默跳过。

默认值不变（不破坏现有测试与缓存指纹）；`auto` 为可选值并写进
`.env.example` 注释与文档。

### 8. 文档（TODO §11）

- docs/06：新增 D-053（connect 连接存储与 Keychain 凭据）、D-054（active
  provider 路由叠加策略与"不硬失败"决定）、D-055（asr.provider=auto）。
- docs/08：roadmap 增加 connect-first 条目并标记本切片完成项。
- README（英文默认 + 中文）：快速开始改为 connect-first 流程。
- CLI_CONNECT_TODO.md 勾选项更新。

## 测试策略（TODO §10）

- store：读写往返、坏 JSON/未知版本/未知 provider/缺字段全部显式报错、原子写、
  不含 apiKey 字段。
- secrets：Memory store 往返、删除；redaction 断言 secret 不出现在 status 输出；
  Keychain 实现用 monkeypatch 伪 subprocess 测试命令构造（不碰真 keychain）。
- registry：qwen/mimo spec 完整性、未知 provider 拒绝。
- CLI：add（非交互 flags）写配置+secret 且 connections.json 无明文 key；
  status 脱敏；switch/remove/list；test 的 health check 用 monkeypatch 拦网络。
- 路由：active provider 叠加到 unified/text 配置组；无 provider 回退 env；
  两者皆无的 warning 含 connect 提示；mock 注入路径不受影响。
- ASR auto：本地可用/不可用、远程齐全/不齐全四条路径。
- 分发回归：shot/story/profile --help 均显示各自完整帮助。
- 全部测试通过 `tmp_path` + 依赖注入，不写真实 HOME、不访问网络。

## 风险

- `--help` 分发修复改变 `shot` 的参数解析路径：REMAINDER 透传与前置分流在
  异常参数上的行为差异需回归测试覆盖（shot 的 e2e 已存在，保持绿）。
- Keychain 非 macOS 平台不可用：默认降级 memory store 会让凭据不持久，
  需要 warning 明示；本期目标平台即 macOS。
- TODO 示例模型名 `qwen3.5-omni` 未经验证（以 TODO 为准，health check 兜底，
  用户可在 add 时覆盖）。
