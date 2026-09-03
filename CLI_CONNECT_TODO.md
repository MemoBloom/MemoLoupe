# MemoLoupe CLI Connect-First TODO

## Background

MemoLoupe already has the core analysis pipeline in place. The current CLI includes:

- `memoloupe shot`
- `memoloupe story`
- `memoloupe profile`
- `memoloupe review`
- `memoloupe import-corrections`
- `memoloupe validate`
- `memoloupe config`

The release-facing CLI still lacks the most important user entry point: `memoloupe connect`.

The product direction is now clear: build `connect` first, and postpone `login`. In the first phase, users should connect their own existing model service APIs, such as Qwen or MiMo. MemoLoupe remains responsible for local deterministic media processing, artifact generation, validation, and review; model inference is routed through the configured provider.

## Goal

Implement a release-ready connect-first CLI so a user can start with:

```bash
memoloupe connect add qwen
memoloupe connect status
memoloupe connect test
memoloupe shot /path/to/video.mp4 --out /path/to/output
memoloupe story /path/to/output
memoloupe profile /path/to/output
```

The user should not need to understand or configure separate ASR, unified media, and text model services. The CLI should present this as one coherent action: connecting MemoLoupe to a model provider.

## Confirmed Product Decisions

- Ship `connect` first.
- Do not implement official account login in this phase.
- Reserve `memoloupe login` for the future official hosted service.
- Do not use arbitrary user aliases such as `my-qwen` or `my-mimo`.
- Use fixed provider IDs:
  - `qwen`
  - `mimo`
  - Later: `openai-compatible`
- Preferred commands:
  - `memoloupe connect add qwen`
  - `memoloupe connect add mimo`
- Local runtime responsibilities:
  - video segmentation
  - audio features
  - optional local ASR
  - artifact generation
  - validation and review
- Do not pursue a pure-local user experience now.
- ASR should not be exposed as a separate user workflow.
- Pipeline ASR routing should be automatic:
  - use local ASR first if available
  - fallback to provider ASR if supported
  - otherwise produce an explicit degraded state

## Current Code State

- Shot, Story, and Profile analysis pipelines already exist.
- Real service layers already include ASR, Unified Media, and Text Model foundations.
- Current config is still developer-oriented and mainly driven by env / `.env`.
- There is no `connect` command yet.
- Known CLI issue:
  - `uv run memoloupe shot --help` currently shows only a shallow top-level help.
  - It does not dispatch into the full `shot_analysis.py` parser.
  - Fix the `shot` dispatch behavior in `src/memoloupe/cli/main.py`.

## Required Pre-Work

Before modifying code, the development agent must read the repository instructions and design docs in this order:

1. `docs/README.md`
2. `docs/00_REPRODUCTION_SPEC.md`
3. `docs/01_ARCHITECTURE_AND_MODULES.md`
4. `docs/02_DATA_AND_STATE_CONTRACTS.md`
5. `docs/03_PIPELINES_AND_ALGORITHMS.md`
6. `docs/04_UI_AND_VALIDATION.md`
7. `docs/05_TESTING_AND_ACCEPTANCE.md`
8. `docs/06_DECISIONS_AND_ASSUMPTIONS.md`
9. `docs/07_SOURCE_DATA_CONTRACT.md`
10. `docs/08_DEVELOPMENT_ROADMAP.md`

Do not weaken existing contracts to make tests pass. If implementation and contract disagree, fix the implementation or explicitly update the contract with a migration strategy.

## 1. Fix CLI Dispatch

- [x] Fix `memoloupe shot --help` so it shows the complete shot command arguments.
- [x] Preserve existing behavior for `story`, `profile`, `review`, and `import-corrections`.
- [x] Add CLI dispatch regression tests.

Acceptance commands:

```bash
uv run memoloupe shot --help
uv run memoloupe story --help
uv run memoloupe profile --help
```

All three commands must show their own complete command-specific help.

## 2. Add `connect` Command Group

Implement:

```bash
memoloupe connect add qwen
memoloupe connect add mimo
memoloupe connect status
memoloupe connect test
memoloupe connect switch qwen
memoloupe connect remove qwen
```

Minimum first delivery:

- [x] `connect add qwen`
- [x] `connect status`
- [x] `connect test`
- [x] automatic active provider loading from pipeline commands

Can be completed afterward:

- [x] `connect switch`
- [x] `connect remove`
- [x] `connect list`

## 3. Add Provider Connection Store

Add a user-level connection store.

Recommended path:

```text
~/.config/memoloupe/connections.json
```

Tests must be able to inject a temporary config path. Tests must not write to the real user home directory.

Suggested schema:

```json
{
  "version": 1,
  "activeProvider": "qwen",
  "providers": {
    "qwen": {
      "providerId": "qwen",
      "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
      "models": {
        "media": "qwen3.5-omni",
        "text": "qwen-plus",
        "asr": null
      },
      "capabilities": {
        "mediaUnderstanding": true,
        "text": true,
        "asr": false
      },
      "createdAt": "...",
      "updatedAt": "..."
    }
  }
}
```

Requirements:

- [x] Do not write API keys in plaintext to `connections.json`.
- [x] Store API keys in OS Keychain by default.
- [x] Provide a mock or memory secret store for CI and tests.
- [x] Use atomic writes for connection config.
- [x] Explicitly reject bad JSON, unknown schema versions, invalid providers, and invalid enum values.

## 4. Add Credential Store

Add a credential abstraction:

- [x] `SecretStore` interface
- [x] macOS Keychain implementation
- [x] env fallback or test memory store
- [x] redaction helper for logs and errors

Recommended Keychain identity:

```text
service: memoloupe
account: provider:qwen
```

Acceptance:

- [x] `connect status` must never display a full secret.
- [x] Unit tests must cover secret redaction.
- [x] Removing a provider should also remove its stored secret.

## 5. Add Provider Registry

Add a central provider registry. It should define:

- provider ID
- default base URL
- recommended models
- supported capabilities
- required fields
- health check behavior
- service adapter construction

Initial providers:

- [x] `qwen`
- [x] `mimo`

Do not scatter provider-specific logic across Shot, Story, and Profile CLI modules.

## 6. Implement `connect add qwen`

Interactive command:

```bash
memoloupe connect add qwen
```

Suggested prompts:

- API key
- base URL, defaulting to Qwen's OpenAI-compatible endpoint
- media model, defaulting to Qwen Omni
- text model, defaulting to a suitable Qwen text model
- whether to make this the active provider, default yes

On completion:

- [x] Save provider config.
- [x] Save secret.
- [x] Run health check.
- [x] Set provider as active after successful setup.
- [x] Print concise next-step commands.

Future non-interactive form:

```bash
memoloupe connect add qwen \
  --api-key-env DASHSCOPE_API_KEY \
  --base-url ... \
  --media-model ... \
  --text-model ...
```

## 7. Implement `connect add mimo`

Interactive command:

```bash
memoloupe connect add mimo
```

Requirements:

- [x] Support MiMo multimodal model configuration.
- [x] Reserve fields for MiMo ASR support.
- [x] If MiMo's API is not fully OpenAI-compatible, implement a MiMo adapter instead of forcing it into a generic adapter.

## 8. Route Pipelines Through Active Provider

Update service resolution for:

- `memoloupe shot`
- `memoloupe story`
- `memoloupe profile`

Requirements:

- [x] If an active provider exists, use it by default.
- [x] If no active provider exists, preserve the existing env config path.
- [x] If neither exists, fail with a clear onboarding message suggesting `memoloupe connect add qwen`.
- [x] Do not require the user to manually pass three separate model configs.
- [x] Preserve test mock service support.

## 9. Add Unified ASR Routing

Add an automatic ASR strategy:

```text
asr = auto
```

Priority:

1. local ASR if available
2. active provider ASR if supported
3. explicit degraded state

Requirements:

- [x] Do not require separate ASR setup for the main workflow.
- [x] Do not silently skip ASR when unavailable.
- [x] Preserve existing degraded / unavailable artifact states.

## 10. Required Tests

The implementation agent must run tests after development. Do not stop after code changes without verification.

Minimum full test command:

```bash
uv run pytest -q tests
```

If the full suite is too slow, at minimum run:

```bash
uv run pytest -q tests/test_cli*
uv run pytest -q tests/test_*config*
uv run pytest -q tests/test_*service*
```

Manual CLI checks:

```bash
uv run memoloupe --help
uv run memoloupe connect --help
uv run memoloupe connect status
uv run memoloupe shot --help
uv run memoloupe story --help
uv run memoloupe profile --help
```

New tests must cover:

- [x] `connect add qwen` writes provider config.
- [x] API secret does not land in `connections.json`.
- [x] `connect status` redacts secrets.
- [x] active provider is picked up by pipeline service resolution.
- [x] no active provider falls back to legacy env config.
- [x] no usable config gives a clear onboarding error.
- [x] bad config, unknown provider, and missing secret errors are explicit.
- [x] `shot --help` dispatch regression.

## 11. Required Documentation Updates

After implementation, update:

- [x] `docs/08_DEVELOPMENT_ROADMAP.md`
- [x] `docs/06_DECISIONS_AND_ASSUMPTIONS.md`
- [x] README quick start

Suggested README quick start:

```bash
uv sync
memoloupe connect add qwen
memoloupe shot ./example.mp4 --out ./out
memoloupe story ./out
memoloupe profile ./out
```

> 状态（2026-09-02）：§1~§11 全部完成（分支 feat/connect-cli，决策 D-053~D-055）。`--out` 别名与 story/profile 位置参数形式未纳入本期，CLI 保持 `--output-dir`。

## 12. Out Of Scope

Do not include these in this development slice:

- [x] `memoloupe login`
- [x] official hosted server
- [x] billing
- [x] user account system
- [x] web dashboard
- [x] automatic deployment of user-owned model servers
- [x] FCPXML export
- [x] Story Spine generation
- [x] automatic rough cut generation

`memoloupe login` only needs to be reserved conceptually.

## Final Acceptance Criteria

The desired first-run user experience is:

```bash
memoloupe connect add qwen
memoloupe shot video.mp4 --out out
memoloupe story out
memoloupe profile out
```

Final delivery must satisfy:

- [x] CLI help is complete.
- [x] provider config is persisted.
- [x] API keys are not stored in plaintext.
- [x] pipelines automatically use the active provider.
- [x] missing provider errors are clear and actionable.
- [x] required tests have been run and pass.
- [x] docs explain the connect-first release path.
