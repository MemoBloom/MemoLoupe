<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="MemoLoupe — one command turns a reference video into shot, story & style profiles, ready to remake">
</p>

<p align="center">
  English | <a href="README.zh-CN.md">中文</a>
</p>

MemoLoupe is a **film-analysis （拉片） tool**: give it a reference video, and it dissects the video into three structured profiles — shots, story, and style — reviewed by you and ready to guide remakes of similar content.

## What you get

| Command | Output | Contents |
|---|---|---|
| `memoloupe shot` | `shot-analysis.html` | Merged analysis (shots + story): dual story/shot timeline workbench — content, camera, lighting and sound per shot, each linked back to its evidence |
| `memoloupe story` | `story-analysis.html` | Story structure (blocks, slots, relations). Runs automatically after `shot`; this standalone command is for re-runs after corrections |
| `memoloupe profile` | `style-profile.json` | Style profile: structure/pacing/style distributions and remake notes (machine-readable contract) |

Every conclusion in the HTML links back to raw evidence (clips, frames, audio segments). Anything the model is unsure about is explicitly marked — never dressed up as fact.

## Quick start

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), ffmpeg. On macOS (Apple Silicon) you can additionally enable local ASR and Apple Vision camera-motion analysis.

```bash
# 1. Install
uv sync
uv sync --extra asr-local   # optional: local ASR (FireRedVAD + MLX Whisper)

# 2. Connect your model provider (interactive; API key goes to the OS
#    Keychain, never to a plaintext file)
uv run memoloupe connect add qwen     # or: connect add mimo
uv run memoloupe connect status       # inspect; connect test runs a health check

# 3. Analyze (shot runs story automatically afterwards — one command for
#    both; pipelines use the active provider, and degrade explicitly
#    without one while deterministic analysis is unaffected)
uv run memoloupe shot    ./video.mp4 --output-dir ./out

# 4. Export the style profile for remakes
uv run memoloupe profile --output-dir ./out

# 5. Validate artifacts (schema + cross-file consistency + HTML semantics)
uv run memoloupe validate ./out --strict

# 6. Review
open ./out/shot-analysis.html                    # merged workbench: shots + story
uv run memoloupe review --output-dir ./out       # localhost review UI
```

Prefer environment variables over `connect`? The legacy env path still works and is used when no active provider exists:

```bash
cp .env.example .env   # fill in MEMOLOUPE_TEXTMODEL__* / MEMOLOUPE_UNIFIEDMODEL__*
uv run memoloupe shot ./video.mp4 --output-dir ./out --env-file .env
```

## CLI reference

Global: every command accepts `--env-file PATH` to load a `.env` file (never overrides variables already set in the environment).

| Command | Purpose | Key options |
|---|---|---|
| `connect add qwen\|mimo` | Connect a model provider; interactive, API key stored in the OS Keychain | `--api-key-env ENV`, `--base-url`, `--media-model`, `--text-model`, `--asr-model` (non-interactive) |
| `connect status` / `test` / `switch` / `remove` / `list` | Inspect, health-check, switch, or delete connections | `test [provider]` defaults to the active one |
| `shot VIDEO --output-dir DIR` | Phase 1+2 merged: shot analysis, then story analysis automatically (`--skip-story` opts out) | `--skip-story`, `--gap-ms N`, `--skip STEP`, `--dry-run`, `--render-only`, `--strict`, `--max-shots N`, `--force STEP`, `--no-cache`, `--align-shot-boundaries-to-audio`, `--mock-services`, `--json-report` |
| `story --output-dir DIR` | Standalone story re-run (e.g. after shot corrections; runs with `shot` by default) | `--allow-draft`, `--scaffold-only`, `--gap-ms N`, `--max-blocks N`, `--mock-text-model`, `--strict` |
| `profile --output-dir DIR` | Phase 3: style profile (expects story artifacts) | `--skip-distill`, `--mock-text-model`, `--strict` |
| `review --output-dir DIR` | localhost human-review UI | `--port 8765` |
| `import-corrections FILE --output-dir DIR` | Import offline corrections and re-render | |
| `validate DIR` | Validate artifacts (schema + cross-file + HTML) | `--strict`, `--json-report` |
| `config` | Print the redacted effective config and list unconfigured services | |

Model configuration resolution order: **active provider (`connect`) → env/`.env` → explicit degrade**. ASR routing via `asr.provider`: `auto` (local first, then remote, else explicit degrade), `local-fireredvad-mlx`, `openai-json` (default), `openai-multipart`, `mimo-chat` (MiMo chat-completions ASR; set automatically by `connect add mimo`).

Exit codes: `0` done (warnings allowed) · `2` usage/config error · `3` input/contract error · `4` ffmpeg/ffprobe unavailable · `5` stage failed · `6` validation failed.

## How it works

1. **Deterministic detection first** — cuts, BGM, audio cut points, quality and camera motion are measured by detectors (Apple Vision, ffmpeg); models cannot override this evidence.
2. **Models only do semantics** — a multimodal model (MiMo or any OpenAI-compatible endpoint) describes shot content / sound / editing function; ASR can run fully locally (FireRedVAD speech separation + MLX Whisper); the text model only distills story and profile from **text** and never receives video or frames.
3. **Controlled vocabulary + five-state values** — semantic fields normalize to a fixed vocabulary, and every value distinguishes "measured absent / model-claimed absent / unverifiable".
4. **Human review loop** — story results merge into the same workbench as a dual timeline; corrections live as an overlay and survive re-runs.

## Scope

Output stops at the artifacts above. Story-spine generation, footage matching, automatic rough cuts and FCPXML export are **out of scope** — they belong to downstream consumers.

## Docs & development

- Design & contract docs: [`docs/`](docs/) (start from `00_REPRODUCTION_SPEC.md`)
- Contributor / AI-agent guide: [`AGENTS.md`](AGENTS.md)
- Tests: `uv run pytest` (unit + contract + integration + e2e)
- Real-service integration tests: opt in via `MEMOLOUPE_RUN_REAL_SERVICE_TESTS=1` (off in CI by default)
