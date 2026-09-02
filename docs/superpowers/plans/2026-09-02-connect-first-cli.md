# Connect-First CLI 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 `memoloupe connect` 命令组，让用户用一条命令连接自己的模型服务（qwen/mimo），管道自动使用 active provider；顺带修复 `shot --help` 分发缺陷。

**Architecture:** 新增 `src/memoloupe/connect/` 包（store/secrets/registry/runtime 四个模块）+ `cli/connect.py`。凭据存 macOS Keychain（stdlib `security` CLI，不引 keyring），provider 配置存 `~/.config/memoloupe/connections.json`（原子写）。管道路由通过在三个 CLI 模块的 `load_config()` 调用点后叠加 `resolve_active_provider()` 实现，服务构造代码不动。

**Tech Stack:** Python 3.12 / uv / pytest；纯标准库（urllib + subprocess）。

**Spec:** `docs/superpowers/specs/2026-09-02-connect-first-cli-design.md`（设计决策与偏离说明以它为准）

## Global Constraints

- 测试基线：`uv run pytest -q` 当前 1161 passed, 8 skipped；每个 Task 结束全量或相关子集必须绿。
- 分支：`feat/connect-cli`（已创建）。commit message 用英文 conventional commit。
- 不得削弱既有契约：schema、五态、`data-*` 语义、指纹缓存、降级矩阵全部不变。
- 凭据永不进日志/异常/快照/connections.json（docs/00 §7.3、docs/01 §4.1）。
- 测试不写真实 HOME、不访问网络（网络一律 monkeypatch）。
- 不引入新的第三方依赖；不实现 login/账号/计费（spec "明确不做"）。
- 退出码沿用 docs/01 §10：0/2/3/4/5/6。
- 代码注释与 docstring 用中文（项目惯例）；CLI 输出文案沿用现有中文风格。

---

### Task 1: 修复 shot --help 分发

**Files:**
- Modify: `src/memoloupe/cli/main.py`（`_dispatch`，约 :224-245）
- Test: `tests/integration/test_cli_dispatch.py`（新建）

**Interfaces:**
- Consumes: 现有 `run_shot_analysis(argv)` / `run_story_analysis(argv)` / `run_profile_build(argv)`
- Produces: `memoloupe shot --help` 输出 shot_analysis.py 的完整帮助（含 `--output-dir`、`--mock-services` 等）；行为变化仅限 help/参数解析入口

- [ ] **Step 1: 写失败测试**

新建 `tests/integration/test_cli_dispatch.py`：

```python
"""CLI 分发回归：shot/story/profile --help 必须显示各自完整帮助。"""

from __future__ import annotations

import pytest

from memoloupe.cli.main import main


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("shot", "--output-dir"),
        ("story", "--scaffold-only"),
        ("profile", "--skip-distill"),
    ],
)
def test_subcommand_help_shows_full_parser(command, expected, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main([command, "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert expected in out
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_cli_dispatch.py -q`
Expected: shot 用例 FAIL（`--output-dir` 不在浅层帮助中），story/profile 通过。

- [ ] **Step 3: 修复分发**

`src/memoloupe/cli/main.py::_dispatch`，在 review 分流前加：

```python
    if argv[:1] == ["shot"]:
        return run_shot_analysis(argv[1:])
```

主 parser 中 shot 子 parser 条目保留（顶层 `--help` 列表需要），但其 REMAINDER 分支从此不可达，可保留作兜底。

- [ ] **Step 4: 运行确认通过 + 既有回归**

Run: `uv run pytest tests/integration/test_cli_dispatch.py tests/e2e -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/memoloupe/cli/main.py tests/integration/test_cli_dispatch.py
git commit -m "fix(cli): route shot through prefix dispatch so --help shows full parser"
```

---

### Task 2: 连接存储 + 凭据存储 + provider 注册表

**Files:**
- Create: `src/memoloupe/connect/__init__.py`
- Create: `src/memoloupe/connect/store.py`
- Create: `src/memoloupe/connect/secrets.py`
- Create: `src/memoloupe/connect/registry.py`
- Test: `tests/unit/test_connect_store.py`、`tests/unit/test_connect_secrets.py`、`tests/unit/test_connect_registry.py`

**Interfaces:**
- Produces（后续任务依赖这些签名，不得更改）：

```python
# connect/store.py
class ConnectionStoreError(MemoLoupeError): ...
class ConnectionStore:
    def __init__(self, path: Path | None = None): ...
    # path=None 时用 default_connections_path()
    def load(self) -> dict: ...          # 文件不存在返回空骨架 {"version":1,"activeProvider":None,"providers":{}}
    def save(self, data: dict) -> None: ...  # 校验 + 原子写
    def upsert_provider(self, record: dict, *, make_active: bool) -> None: ...
    def remove_provider(self, provider_id: str) -> None: ...
    def set_active(self, provider_id: str) -> None: ...
    def get_active(self) -> dict | None: ...

def default_connections_path() -> Path: ...
# ~/.config/memoloupe/connections.json；env MEMOLOUPE_CONNECTIONS_PATH 覆盖

# connect/secrets.py
class SecretStore(Protocol):
    def get(self, provider_id: str) -> str | None: ...
    def set(self, provider_id: str, secret: str) -> None: ...
    def delete(self, provider_id: str) -> None: ...

class KeychainSecretStore: ...   # macOS /usr/bin/security，service="memoloupe"，account="provider:<id>"
class MemorySecretStore: ...     # 进程内 dict

def default_secret_store() -> SecretStore: ...
# MEMOLOUPE_SECRET_STORE=memory 强制 memory；macOS 且 security 可用 → Keychain；否则 memory + warning
def redact_secret(text: str, secret: str | None) -> str: ...  # 包装 services.base.redact_text

# connect/registry.py
@dataclass(frozen=True)
class ProviderSpec:
    provider_id: str
    label: str
    default_base_url: str
    default_media_model: str
    default_text_model: str
    default_asr_model: str | None
    capabilities: dict[str, bool]   # mediaUnderstanding/text/asr
    health_check_path: str          # "/models"

PROVIDERS: dict[str, ProviderSpec]  # {"qwen": ..., "mimo": ...}
def get_provider_spec(provider_id: str) -> ProviderSpec: ...  # 未知 id 抛 ConnectionStoreError
```

- provider 默认值：qwen = `https://dashscope.aliyuncs.com/compatible-mode/v1` / media `qwen3.5-omni` / text `qwen-plus` / asr None / caps `{mediaUnderstanding: True, text: True, asr: False}`；mimo = `https://api.xiaomimimo.com/v1` / media `mimo-2.5` / text `mimo-v2.5` / asr None / caps 同 qwen。

- [ ] **Step 1: 写失败测试**（三个测试文件）

store 测试要点（`tmp_path` 注入路径）：
- 不存在文件 → `load()` 返回空骨架
- `upsert_provider` + `save`/`load` 往返一致；`make_active=True` 时 activeProvider 更新
- 坏 JSON、`version=2`、未知 providerId、activeProvider 指向不存在 provider、缺必填字段 → 抛 `ConnectionStoreError`
- 保存的 JSON 序列化后不含传入的 apiKey（构造时就不接受 apiKey 字段——`upsert_provider` 对含 `apiKey` 的 record 抛错）
- `remove_provider` 同时清掉指向它的 activeProvider

secrets 测试要点：
- `MemorySecretStore` set/get/delete 往返；get 不存在返回 None
- `KeychainSecretStore` 用 monkeypatch 替换 `subprocess.run`，断言调用的 argv 含 `memoloupe` 与 `provider:qwen`；find 返回非零时 get 返回 None
- `redact_secret("key is sk-123", "sk-123")` 不出现 `sk-123`
- `default_secret_store`：`MEMOLOUPE_SECRET_STORE=memory` 时返回 MemorySecretStore

registry 测试要点：
- `PROVIDERS` 恰好含 qwen/mimo；两个 spec 字段完整、capabilities 是 bool
- `get_provider_spec("openai-compatible")` 抛错并提示支持的 id 列表

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_connect_store.py tests/unit/test_connect_secrets.py tests/unit/test_connect_registry.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现三个模块**

要点：
- `store.py`：复用 `core/atomic_io.read_json/write_json_atomic`；校验函数 `_validate(data)` 集中所有显式拒绝；provider record 必填 `providerId/baseUrl/models/capabilities`，`models` 必填 `media`/`text` 键（asr 可为 None）；写文件权限 `chmod 0o600`。
- `secrets.py`：Keychain 用 `subprocess.run(["/usr/bin/security", ...], capture_output=True, text=True, check=False)`；add 用 `add-generic-password -U -s memoloupe -a provider:<id> -w <secret>`（secret 走 argv 符合 security CLI 惯例；绝不进日志）；find 用 `find-generic-password -s memoloupe -a provider:<id> -w`；delete 用 `delete-generic-password`，找不到不算错误。
- `registry.py`：纯数据 + 查找。
- `connect/__init__.py`：re-export 公共接口。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/unit/test_connect_*.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/memoloupe/connect tests/unit/test_connect_store.py tests/unit/test_connect_secrets.py tests/unit/test_connect_registry.py
git commit -m "feat(connect): connection store, secret store and provider registry"
```

---

### Task 3: connect CLI 命令组

**Files:**
- Create: `src/memoloupe/cli/connect.py`
- Modify: `src/memoloupe/cli/main.py`（`_dispatch` 前置分流 + 顶层 help 列表）
- Test: `tests/integration/test_connect_cli.py`（新建）

**Interfaces:**
- Consumes: Task 2 的 `ConnectionStore` / `SecretStore` / `get_provider_spec` / `PROVIDERS`
- Produces: `run_connect(argv: Sequence[str]) -> int`；`_dispatch` 识别 `connect` 前缀；`memoloupe --help` 列出 connect

- [ ] **Step 1: 写失败测试**

`tests/integration/test_connect_cli.py`，全部用 `tmp_path` 构造 `ConnectionStore` + `MemorySecretStore` 注入（`run_connect` 接受可选 `store=`/`secrets=` 参数，默认 None 走全局默认），health check 用 monkeypatch 拦 `connect` 模块内的 HTTP 函数：

- `add qwen --api-key-env TEST_KEY`（env 注入假 key）→ store 有 qwen 记录、activeProvider=qwen、secret 在 memory store、**connections.json 文本不含假 key**、health check 通过后输出含下一步命令提示
- `add` 缺 key（非交互且无 --api-key-env）→ 退出码 2 + 清晰报错
- `status` 无 provider → 提示 `connect add qwen`；有 provider → 输出含 providerId/baseUrl/models、"secret: 已保存"，且**全文不含假 key**
- `test` health check 成功 → 0；失败 → 5；无 provider → 3 + onboarding 提示
- `switch qwen` / `remove qwen`（remove 后 secret 也被删）/ `list`
- 未知 provider `add foo` → 退出码 2

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/integration/test_connect_cli.py -q`
Expected: FAIL

- [ ] **Step 3: 实现 cli/connect.py**

- health check：`GET {baseUrl}/models`，`Authorization: Bearer <key>`，超时 15s，用 stdlib urllib；2xx 通过；网络/非 2xx → 失败但只报状态码与脱敏错误（复用 `services/base.py` 的 `redact_text`）。
- `add` 交互模式：`getpass.getpass` 收 key，其余 `input(f"... [{default}]")` 回车取默认；非 TTY 且无 `--api-key-env` 时按缺 key 报错（不挂起）。
- `add` 流程：校验 providerId → 收集参数（flags 优先，缺失则交互）→ `store.upsert_provider(record, make_active=False)` → `secrets.set()` → health check → 通过则 `store.set_active()` 并打印 `memoloupe shot video.mp4 --output-dir out` 等下一步；失败打 warning（退出码仍 0）。
- `main.py::_dispatch` 加 `if argv[:1] == ["connect"]: return run_connect(argv[1:])`；`_build_parser` 加 `sub.add_parser("connect", help="连接模型服务提供商（qwen/mimo）")`。

- [ ] **Step 4: 运行确认通过 + CLI 手测**

```bash
uv run pytest tests/integration/test_connect_cli.py tests/integration/test_cli_dispatch.py -q
uv run memoloupe --help
uv run memoloupe connect --help
MEMOLOUPE_CONNECTIONS_PATH=/tmp/ml-conn.json MEMOLOUPE_SECRET_STORE=memory uv run memoloupe connect status
```

Expected: 测试 PASS；help 正常；status 输出 onboarding 提示且退出码 0。

- [ ] **Step 5: Commit**

```bash
git add src/memoloupe/cli/connect.py src/memoloupe/cli/main.py tests/integration/test_connect_cli.py
git commit -m "feat(cli): connect command group (add/status/test/switch/remove/list)"
```

---

### Task 4: 管道路由走 active provider

**Files:**
- Create: `src/memoloupe/connect/runtime.py`
- Modify: `src/memoloupe/cli/shot_analysis.py`、`src/memoloupe/cli/story_analysis.py`、`src/memoloupe/cli/profile_build.py`（各一处 `load_config()` 调用点）
- Test: `tests/unit/test_connect_runtime.py`（新建）、`tests/integration/test_connect_routing.py`（新建）

**Interfaces:**
- Consumes: Task 2 store/secrets/registry
- Produces:

```python
# connect/runtime.py
def resolve_active_provider(
    config: dict,
    *,
    store: ConnectionStore | None = None,
    secrets: SecretStore | None = None,
) -> tuple[dict, str]:
    """active provider 叠加到 unifiedModel/textModel(/asr) 配置组。

    返回 (新 config, source)；source ∈ "provider" | "env" | "none"。
    provider 存在但缺 secret → 抛 ConnectionStoreError（提示 connect test / 重新 add）。
    """
```

- [ ] **Step 1: 写失败测试**

runtime 单测：
- 有 active provider + secret：返回 config 的 `unifiedModel.baseUrl/apiKey/model`、`textModel.*` 被 provider 值覆盖，其余分组不变；source="provider"；原 config dict 不被修改
- provider 不支持 asr → asr 组保持原样
- 无 active provider → source="env"（unifiedModel env 配置完整时）且 config 原样
- 两者皆无 → source="none"，config 原样
- active provider 存在但 secret 缺失 → 显式报错

集成测试（`test_connect_routing.py`）：monkeypatch 三个 CLI 模块的 store/secrets 注入点（或在 CLI 模块加 `store`/`secrets` 可选参数透传），用 fixture output-dir 验证：
- active provider 存在时 story/profile 的 `build_text_model_service` 收到 provider 的 baseUrl/key/model
- 无 provider 时走 env（既有行为回归）
- 两者皆无时 warning 文案含 `memoloupe connect add`
- `--mock-text-model` / `--mock-services` 路径完全不受影响

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_connect_runtime.py tests/integration/test_connect_routing.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

- `runtime.py`：纯函数叠加；叠加用 deep merge 的新 dict（不改入参）；provider 的 models.media → `unifiedModel.model`，models.text → `textModel.model`，baseUrl/key 同理；capabilities.asr=True 且 models.asr 非空时叠加 asr 组（baseUrl/apiKey/model + provider 保持远程 transport 原值或默认 openai-json——注意 asr.provider 是 transport 语义，不覆盖）。
- 三个 CLI 模块：`load_config()` 之后调 `resolve_active_provider(config)`；source="none" 时把现有"未配置"warning 文案改为含 `memoloupe connect add qwen` 的 onboarding 提示（保持降级不中断）。
- shot 管道的 `_build_unified_service`/`_build_asr_service` 不改（它们收到的 config 已被叠加）。

- [ ] **Step 4: 运行确认通过 + 全量**

Run: `uv run pytest tests/unit/test_connect_runtime.py tests/integration/test_connect_routing.py tests/integration/test_story_cli.py -q && uv run pytest -q`
Expected: PASS（全量 1161+新增）

- [ ] **Step 5: Commit**

```bash
git add src/memoloupe/connect/runtime.py src/memoloupe/cli/shot_analysis.py src/memoloupe/cli/story_analysis.py src/memoloupe/cli/profile_build.py tests/unit/test_connect_runtime.py tests/integration/test_connect_routing.py
git commit -m "feat(connect): route pipelines through the active provider"
```

---

### Task 5: ASR auto 路由

**Files:**
- Modify: `src/memoloupe/services/asr.py`（`build_asr_service`）
- Test: `tests/unit/test_asr.py`（追加）或新建 `tests/unit/test_asr_auto.py`

**Interfaces:**
- Consumes: 现有 `build_asr_service(config)` 工厂、`_remote_service_is_configured` 语义
- Produces: `asr.provider == "auto"` 为合法值；路由顺序：本地依赖可用 → `LocalFireRedVadMlxASR`；远程三项齐全 → 现有远程构造；否则 None + 显式 warning

- [ ] **Step 1: 写失败测试**

- `provider="auto"` 且 monkeypatch 本地依赖可用（拦截本地可用性探测函数）→ 返回本地服务实例
- `provider="auto"` 本地不可用、远程齐全 → 返回远程服务
- `provider="auto"` 两者皆无 → None，且 caplog/输出含非静默说明
- 既有 provider 值（openai-json/openai-multipart/local-fireredvad-mlx）行为回归不变

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/unit/test_asr_auto.py -q`
Expected: FAIL

- [ ] **Step 3: 实现**

`build_asr_service` 开头处理 `provider == "auto"`：本地可用性探测（`importlib.util.find_spec("fireredvad")` 与 `find_spec("mlx_whisper")` 均非 None）→ 构造 `LocalFireRedVadMlxASR`（与现有 local 分支同参）；否则远程三项齐全 → 走现有远程分支；否则 `log_warning`（中文说明 + connect 提示）并返回 None。

- [ ] **Step 4: 运行确认通过 + 相关回归**

Run: `uv run pytest tests/unit/test_asr_auto.py tests/unit/test_asr_local.py tests/unit/test_config.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/memoloupe/services/asr.py tests/unit/test_asr_auto.py
git commit -m "feat(asr): auto provider routing (local first, remote fallback, explicit degrade)"
```

---

### Task 6: 文档 + README + 全量验证

**Files:**
- Modify: `docs/06_DECISIONS_AND_ASSUMPTIONS.md`（追加 D-053/D-054/D-055）
- Modify: `docs/08_DEVELOPMENT_ROADMAP.md`（connect-first 条目）
- Modify: `README.md`、`README.zh-CN.md`（快速开始改 connect-first）
- Modify: `.env.example`（asr auto 注释）
- Modify: `CLI_CONNECT_TODO.md`（勾选已完成项）

- [ ] **Step 1: docs/06 追加三条决策**

D-053：connect 连接存储与 Keychain 凭据（路径、schema v1、stdlib security CLI、不引 keyring、memory store 测试注入）。
D-054：active provider 路由叠加策略（叠加点在三 CLI 的 load_config 后；服务构造不动；"none" 时保持降级契约、warning 改 onboarding 文案而不硬失败的理由）。
D-055：`asr.provider=auto`（local first → remote → 显式降级；默认值不变的理由）。

- [ ] **Step 2: docs/08 更新**

在 roadmap 中记录 connect-first 切片（对应 TODO §1-§9 的完成状态，login 保留为未来项）。

- [ ] **Step 3: README 快速开始更新**（英文为主、中文同步）

把 Quick start 的第 2 步前插入 connect 流程：

```bash
uv run memoloupe connect add qwen   # connect your model provider (interactive)
```

并说明：不配 provider 时仍可运行（确定性分析 + 显式降级），env 配置路径保留。

- [ ] **Step 4: CLI_CONNECT_TODO.md 勾选**

把 §1/§2 最小交付/§3/§4/§5/§6/§7/§8/§9/§10/§11 中已完成项勾掉；`--out` 别名与 story/profile 位置参数保持未勾选并注明"未纳入本期"。

- [ ] **Step 5: 全量验证 + 手工 CLI 检查**

```bash
uv run pytest -q
uv run memoloupe --help
uv run memoloupe connect --help
uv run memoloupe shot --help
uv run memoloupe story --help
uv run memoloupe profile --help
MEMOLOUPE_CONNECTIONS_PATH=/tmp/ml-conn-check.json MEMOLOUPE_SECRET_STORE=memory uv run memoloupe connect status
```

Expected: 全量 PASS；各 help 完整；status 输出 onboarding 提示。

- [ ] **Step 6: Commit**

```bash
git add docs/06_DECISIONS_AND_ASSUMPTIONS.md docs/08_DEVELOPMENT_ROADMAP.md README.md README.zh-CN.md .env.example CLI_CONNECT_TODO.md
git commit -m "docs: connect-first decisions (D-053~D-055), roadmap and README quick start"
```
