# 短镜头模型代理改用图像输入 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 短于 2000ms 的镜头不再生成 tpad/apad 补齐视频代理，改用中点帧 JPG 图像代理，transport 层按文件类型发送 `image_url`/`video_url` part，统一对 qwen 与 mimo 生效。

**Architecture:** 模态切换收敛在构建层 `media/clips.py`（`<2s` → 中点帧 jpg，`≥2s` → 视频代理不变）；transport 层 `services/unified_media.py` 按 `proxy_path` 后缀选择 part 类型；unified-media 契约升到 schemaVersion 3（`clipTransport: mediaDataURI`、`shortClipPolicy` 改形）。

**Tech Stack:** Python 3、ffmpeg/ffprobe、pytest、jsonschema。

**Spec:** `docs/superpowers/specs/2026-09-03-short-clip-image-proxy-design.md`

## Global Constraints

- 所有时间使用整数毫秒；镜头区间为 `[startMs, endMs)`。
- 每个呈现值必须可追溯 raw 证据；`modelNormalization.strategy` 必须标明代理形态。
- JSON 写入走临时文件 + 原子替换（本计划不涉及新写路径，沿用现有）。
- 不得静默吞掉旧版本：unified-media schemaVersion 2 → 3，v2 产物不再通过校验（迁移策略记录在 D-059）。
- `CLIP_BUILD_VERSION` 升为 `clips.v4`，旧代理缓存失效。
- 测试运行命令统一为 `.venv/bin/python -m pytest ...`。
- 提交信息遵循仓库现有 conventional commits 风格（如 `feat(media): ...`）。

---

### Task 1: 构建层模态切换（`media/clips.py`）

**Files:**
- Modify: `src/memoloupe/media/clips.py`（全文件改造）
- Test: `tests/unit/test_clips.py`

**Interfaces:**
- Consumes: `memoloupe.media.frames.representative_time_ms(final_start_ms: int, final_end_ms: int) -> int`（已存在，中点+夹紧）。
- Produces（后续任务依赖的精确签名）:
  - `CLIP_BUILD_VERSION = "clips.v4"`
  - `SHORT_CLIP_MS = 2000`（语义改为模态切换阈值）
  - `IMAGE_PROXY_JPEG_QUALITY = 3`
  - `image_proxy_file_rel(shot_id: str, cache_key4: str) -> str` → `clips/model-proxy/{shotID}-{key4}.jpg`
  - `image_proxy_argv(ffmpeg: str, source: str, time_ms: int, out_path: str) -> list[str]`
  - `model_proxy_argv(ffmpeg, source, start_ms, end_ms, out_path, *, has_audio: bool) -> list[str]`（删除 `pad_sec` 参数）
  - `model_normalization(*, cache_key: str, file: str, kind: str) -> dict`（`kind` 为 `"video"`/`"image"`；返回 `{"strategy", "cacheKey", "file"}`，不再有 `padded` 键）
  - 删除 `PADDED_MIN_MS`、`proxy_needs_padding`、`proxy_pad_duration_sec`

- [ ] **Step 1: 重写失败测试 `tests/unit/test_clips.py`（整文件替换）**

```python
"""media/clips.py 单元测试：路径、模态切换、argv 结构与 normalization（不跑 ffmpeg）。"""

from __future__ import annotations

from memoloupe.media.clips import (
    CLIP_BUILD_VERSION,
    SHORT_CLIP_MS,
    clip_file_rel,
    evidence_clip_argv,
    image_proxy_argv,
    image_proxy_file_rel,
    model_normalization,
    model_proxy_argv,
    proxy_file_rel,
)


def test_version_constant() -> None:
    assert CLIP_BUILD_VERSION == "clips.v4"


def test_short_clip_threshold() -> None:
    # 模态切换阈值 2000ms（qwen3.8-flash 视频输入 ≥2s，D-058/D-059）
    assert SHORT_CLIP_MS == 2000


def test_clip_paths_forward_slash() -> None:
    assert clip_file_rel("SH0001") == "clips/SH0001.mp4"
    assert proxy_file_rel("SH0002", "a1b2") == "clips/model-proxy/SH0002-a1b2.mp4"
    assert image_proxy_file_rel("SH0003", "a1b2") == "clips/model-proxy/SH0003-a1b2.jpg"
    assert "\\" not in image_proxy_file_rel("SH0003", "a1b2")


def test_evidence_argv_reencodes_no_stream_copy() -> None:
    argv = evidence_clip_argv(
        "ffmpeg", "/in/src.mp4", 0, 3203, "/out/clips/SH0001.mp4", has_audio=True
    )
    text = " ".join(argv)
    assert "-ss 0.000" in text and "-to 3.203" in text
    assert "libx264" in text and "aac" in text
    assert "copy" not in text  # 禁止 keyframe copy 漂移
    no_audio = evidence_clip_argv(
        "ffmpeg", "/in/src.mp4", 0, 1000, "/out/clips/SH0001.mp4", has_audio=False
    )
    assert "-an" in no_audio


def test_proxy_argv_normalization_no_padding() -> None:
    argv = model_proxy_argv(
        "ffmpeg", "/in/src.mp4", 0, 3203, "/out/p.mp4", has_audio=True
    )
    text = " ".join(argv)
    assert "scale=720:-2" in text
    assert "fps=10" in text
    assert "tpad" not in text
    assert "apad" not in text
    assert "-shortest" not in argv
    assert "+faststart" in argv
    assert "libx264" in text and "aac" in text
    no_audio = model_proxy_argv(
        "ffmpeg", "/in/src.mp4", 0, 3203, "/out/p.mp4", has_audio=False
    )
    assert "-an" in no_audio


def test_image_proxy_argv_midframe_jpg() -> None:
    argv = image_proxy_argv("ffmpeg", "/in/src.mp4", 500, "/out/p.jpg")
    text = " ".join(argv)
    assert "-ss 0.500" in text
    assert "-frames:v 1" in text
    assert "scale=720:-2" in text
    assert "-q:v 3" in text
    assert argv[-1] == "/out/p.jpg"


def test_model_normalization_kinds() -> None:
    video = model_normalization(
        cache_key="proxy-a1b2", file="clips/model-proxy/SH0001-a1b2.mp4", kind="video"
    )
    assert video == {
        "strategy": "reencode-w720-fps10",
        "cacheKey": "proxy-a1b2",
        "file": "clips/model-proxy/SH0001-a1b2.mp4",
    }
    image = model_normalization(
        cache_key="proxy-a1b2", file="clips/model-proxy/SH0001-a1b2.jpg", kind="image"
    )
    assert image == {
        "strategy": "frame-midpoint-w720",
        "cacheKey": "proxy-a1b2",
        "file": "clips/model-proxy/SH0001-a1b2.jpg",
    }
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_clips.py -q`
Expected: FAIL（`ImportError: cannot import name 'image_proxy_file_rel'`）

- [ ] **Step 3: 改造 `src/memoloupe/media/clips.py`**

模块 docstring 改为：

```python
"""证据 clip 与模型代理构建（docs/03 §2.6，unified-media.json 的 clips[]）。

- 证据 clip：按 final 区间精确重编码（libx264/aac），避免 keyframe copy 漂移；
- 模型代理：统一宽 720。短于 2000ms 的镜头输出中点帧 JPG 图像代理
  （静帧对短镜头更具代表性，且绕开云端模型的最短视频约束，D-059）；
  其余输出 fps 10 的重编码视频代理。代理只影响模型输入，不改变证据
  clip 和镜头边界。
"""
```

常量与导入（替换原 `PADDED_MIN_MS` 段）：

```python
from memoloupe.media.frames import representative_time_ms

CLIP_BUILD_VERSION = "clips.v4"

# 模型代理统一参数（docs/03 §2.6 恢复策略）
PROXY_WIDTH = 720
PROXY_FPS = 10
#: 模态切换阈值：低于 2000ms 的镜头用中点帧图像代理（qwen3.8-flash 要求
#: 视频输入 ≥2s，D-058；短镜头改用图像由 D-059 决策）。
SHORT_CLIP_MS = 2000
#: 图像代理 JPEG 质量（ffmpeg -q:v，越小越好）
IMAGE_PROXY_JPEG_QUALITY = 3
```

删除 `proxy_needs_padding` 与 `proxy_pad_duration_sec`，新增：

```python
def image_proxy_file_rel(shot_id: str, cache_key4: str) -> str:
    return f"clips/model-proxy/{validate_shot_id(shot_id)}-{cache_key4}.jpg"
```

`model_proxy_argv` 删除 `pad_sec` 参数与 tpad/apad 分支，函数体为：

```python
def model_proxy_argv(
    ffmpeg: str,
    source: str,
    start_ms: int,
    end_ms: int,
    out_path: str,
    *,
    has_audio: bool,
) -> list[str]:
    """模型代理：宽 720、fps 10，输出 faststart MP4。"""
    vf = f"scale={PROXY_WIDTH}:-2,fps={PROXY_FPS}"
    argv = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", _sec(start_ms), "-to", _sec(end_ms), "-i", source,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-pix_fmt", "yuv420p",
    ]
    if has_audio:
        argv += ["-c:a", "aac", "-b:a", "96k"]
    else:
        argv += ["-an"]
    argv += ["-movflags", "+faststart"]
    argv.append(out_path)
    return argv


def image_proxy_argv(ffmpeg: str, source: str, time_ms: int, out_path: str) -> list[str]:
    """图像代理：镜头中点单帧，宽 720 JPEG。"""
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{time_ms / 1000:.3f}", "-i", source,
        "-frames:v", "1",
        "-vf", f"scale={PROXY_WIDTH}:-2",
        "-q:v", str(IMAGE_PROXY_JPEG_QUALITY),
        out_path,
    ]
```

`model_normalization` 改为：

```python
def model_normalization(*, cache_key: str, file: str, kind: str) -> dict:
    if kind == "image":
        strategy = f"frame-midpoint-w{PROXY_WIDTH}"
    else:
        strategy = f"reencode-w{PROXY_WIDTH}-fps{PROXY_FPS}"
    return {
        "strategy": strategy,
        "cacheKey": cache_key,
        "file": file,
    }
```

`build_clips` 循环体中代理构建段（替换 `proxy_rel = ...` 到 `model_duration_ms = ...`）：

```python
        if duration_ms < SHORT_CLIP_MS:
            frame_ms = representative_time_ms(start_ms, end_ms)
            proxy_rel = image_proxy_file_rel(shot_id, revision4)
            proxy_path = proxy_dir / f"{shot_id}-{revision4}.jpg"
            pool.run(
                image_proxy_argv(ffmpeg, str(source), frame_ms, str(proxy_path)),
                timeout_sec=timeout,
            )
            # 静帧没有可探测时长；语义为"模型输入所代表的镜头时长"
            model_duration_ms = duration_ms
            kind = "image"
        else:
            proxy_rel = proxy_file_rel(shot_id, revision4)
            proxy_path = proxy_dir / f"{shot_id}-{revision4}.mp4"
            pool.run(
                model_proxy_argv(
                    ffmpeg, str(source), start_ms, end_ms, str(proxy_path),
                    has_audio=has_audio,
                ),
                timeout_sec=timeout,
            )
            model_duration_ms = _probe_duration_ms(proxy_path, config, pool)
            kind = "video"
```

`items.append` 中 `modelNormalization` 调用改为：

```python
                "modelNormalization": model_normalization(
                    cache_key=cache_key, file=proxy_rel, kind=kind
                ),
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_clips.py -q`
Expected: PASS（7 个用例）

- [ ] **Step 5: 提交**

```bash
git add src/memoloupe/media/clips.py tests/unit/test_clips.py
git commit -m "feat(media): short clips (<2s) use mid-frame image proxy (clips.v4)"
```

---

### Task 2: Transport 层图像 part（`services/unified_media.py`）

**Files:**
- Modify: `src/memoloupe/services/unified_media.py`
- Test: `tests/unit/test_services_unified.py`

**Interfaces:**
- Consumes: `ModelClip`（`shot_id: str`、`proxy_path: Path`、`duration_ms: int`，由 `media_orchestrator.py:433` 构造，本任务不改构造方）。
- Produces: `ModelClip.is_image: bool`（property，`proxy_path` 后缀 ∈ `{.jpg, .jpeg}`，大小写不敏感）；`analyze_batch` 对图像 clip 发 `{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}` part（无 `fps`/`media_resolution`）。

- [ ] **Step 1: 写失败测试**

在 `tests/unit/test_services_unified.py` 的 `TestAnalyzeBatch` 类中新增，并修改 `test_request_payload_shape` 中两行映射断言（见 Step 3 的最终文本）：

```python
    def test_image_clip_uses_image_url_part(self, server, tmp_path):
        img = tmp_path / "SH0003.jpg"
        img.write_bytes(b"jpeg-bytes")
        clips = _clips(tmp_path) + [
            ModelClip(shot_id="SH0003", proxy_path=img, duration_ms=600)
        ]
        server.handler.behavior = {"body": _chat_response("{}")}
        _make(server).analyze_batch(clips, _group())
        payload = json.loads(server.handler.captured["body"])
        content = payload["messages"][0]["content"]
        image_parts = [p for p in content if p["type"] == "image_url"]
        assert len(image_parts) == 1
        part = image_parts[0]
        assert "fps" not in part and "media_resolution" not in part
        url = part["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        assert base64.b64decode(url.split(",", 1)[1]) == b"jpeg-bytes"
        request_text = content[-1]["text"]
        assert "第 3 个 image_url（静态图像） = SH0003" in request_text

    def test_is_image_property(self, tmp_path):
        assert ModelClip(
            shot_id="SH0001", proxy_path=tmp_path / "a.JPG", duration_ms=1
        ).is_image
        assert not ModelClip(
            shot_id="SH0001", proxy_path=tmp_path / "a.mp4", duration_ms=1
        ).is_image
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_services_unified.py -q -k "image_clip or is_image"`
Expected: FAIL（`AttributeError: 'ModelClip' object has no attribute 'is_image'`）

- [ ] **Step 3: 实现**

`src/memoloupe/services/unified_media.py`：

常量区（`_VIDEO_MIME` 旁）新增：

```python
_IMAGE_MIME = "image/jpeg"
_IMAGE_SUFFIXES = {".jpg", ".jpeg"}
```

`ModelClip` 增加 property：

```python
@dataclass(frozen=True)
class ModelClip:
    """一个待分析的镜头模型代理 clip。"""

    shot_id: str
    proxy_path: Path  # clips/model-proxy/ 下的文件（.mp4 或 .jpg）
    duration_ms: int

    @property
    def is_image(self) -> bool:
        """图像代理（短镜头中点帧）→ image_url part；否则 video_url。"""
        return self.proxy_path.suffix.lower() in _IMAGE_SUFFIXES
```

`analyze_batch` 的 content 构造段替换为：

```python
        # MiMo 官方视频示例以媒体 parts 在前、text part 在后；保持该顺序。
        # 短镜头的图像代理走 image_url，不带 fps/media_resolution。
        content: list[dict] = []
        total_bytes = 0
        for clip in clips:
            try:
                data = clip.proxy_path.read_bytes()
            except OSError as exc:
                raise PermanentServiceError(
                    f"clip unreadable: shotID={clip.shot_id} {type(exc).__name__}"
                ) from None
            total_bytes += len(data)
            encoded = base64.b64encode(data).decode("ascii")
            if clip.is_image:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{_IMAGE_MIME};base64," + encoded
                        },
                    }
                )
            else:
                content.append(
                    {
                        "type": "video_url",
                        "video_url": {
                            "url": f"data:{_VIDEO_MIME};base64," + encoded
                        },
                        "fps": self._video_fps,
                        "media_resolution": self._media_resolution,
                    }
                )
        shot_mapping = "\n".join(
            f"- 第 {index} 个 "
            f"{'image_url（静态图像）' if clip.is_image else 'video_url（视频）'}"
            f" = {clip.shot_id}"
            for index, clip in enumerate(clips, 1)
        )
        request_prompt = (
            f"{group.prompt}\n"
            "本批次输入媒体（视频或静态图像）与 shotID 的唯一映射如下"
            "（严格按 content 顺序）：\n"
            f"{shot_mapping}\n"
            "不要把媒体内部时间点当成镜头；静态图像输入代表整个短镜头。"
            "shots 数组只能使用上述 shotID，并且每个 shotID 恰好返回一次。"
        )
```

类 docstring 中"每个 clip 读文件转 base64 ``video/mp4`` Data URI"一句更新为"每个 clip 读文件转 base64 Data URI（视频为 ``video/mp4``，短镜头图像代理为 ``image/jpeg``）"。

同步修改 `test_request_payload_shape` 中的两行断言：

```python
        assert "第 1 个 video_url（视频） = SH0001" in request_text["text"]
        assert "第 2 个 video_url（视频） = SH0002" in request_text["text"]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/unit/test_services_unified.py -q`
Expected: PASS（全部用例）

- [ ] **Step 5: 提交**

```bash
git add src/memoloupe/services/unified_media.py tests/unit/test_services_unified.py
git commit -m "feat(unified): image_url part for short-clip image proxies"
```

---

### Task 3: unified-media 契约 v3（schema + 生产者 + 夹具）

**Files:**
- Modify: `schemas/unified-media.json`（schemaVersion const、clipTransport const、shortClipPolicy）
- Modify: `src/memoloupe/analysis/shot_pipeline.py:437-456`（skipped stub 文档）
- Modify: `src/memoloupe/analysis/media_orchestrator.py:546-567`（正式 document）与第 52 行 import
- Modify: `tests/fixtures/output_full/raw/unified-media.json`、`tests/fixtures/minimal/raw/unified-media.json`
- Test: `tests/unit/test_media_orchestrator.py:103-113`；`tests/contract/test_cross_artifact.py`（自动校验夹具）

**Interfaces:**
- Consumes: Task 1 的 `SHORT_CLIP_MS`、`PROXY_WIDTH`。
- Produces: unified-media 文档 `schemaVersion: 3`、`request.clipTransport: "mediaDataURI"`、`request.shortClipPolicy: {"minimumDurationMs": 2000, "imageProxyWidth": 720}`。

- [ ] **Step 1: 写失败测试**

`tests/unit/test_media_orchestrator.py` 中 `test_complete_run_passes_schema` 的断言改为：

```python
        assert request["clipTransport"] == "mediaDataURI"
        ...
        assert request["shortClipPolicy"] == {
            "minimumDurationMs": 2000,
            "imageProxyWidth": 720,
        }
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/unit/test_media_orchestrator.py -q`
Expected: FAIL（`AssertionError` on clipTransport / shortClipPolicy）

- [ ] **Step 3: 改 schema 与两个生产者**

`schemas/unified-media.json`：
- `"schemaVersion": { "const": 2 }` → `"schemaVersion": { "const": 3 }`
- `"clipTransport": { "const": "videoDataURI" }` → `"clipTransport": { "const": "mediaDataURI" }`
- `shortClipPolicy` 改为：

```json
        "shortClipPolicy": {
          "type": "object",
          "additionalProperties": true,
          "required": [
            "minimumDurationMs",
            "imageProxyWidth"
          ],
          "properties": {
            "minimumDurationMs": { "type": "integer", "minimum": 0 },
            "imageProxyWidth": { "type": "integer", "minimum": 1 }
          }
        }
```

`src/memoloupe/analysis/shot_pipeline.py`（stub 文档，约 437-456 行）：
- `"schemaVersion": 2` → `"schemaVersion": 3`
- `"clipTransport": "videoDataURI"` → `"clipTransport": "mediaDataURI"`
- `shortClipPolicy` 改为：

```python
            "shortClipPolicy": {
                "minimumDurationMs": SHORT_CLIP_MS,
                "imageProxyWidth": PROXY_WIDTH,
            },
```

- 顶部 import：`from memoloupe.media.clips import (CLIP_BUILD_VERSION, PADDED_MIN_MS, SHORT_CLIP_MS)` 改为导入 `CLIP_BUILD_VERSION, PROXY_WIDTH, SHORT_CLIP_MS`。

`src/memoloupe/analysis/media_orchestrator.py`（约 546-567 行）：
- `"schemaVersion": 2` → `"schemaVersion": 3`
- `"clipTransport": "videoDataURI"` → `"clipTransport": "mediaDataURI"`
- `shortClipPolicy` 同上新形态
- 第 52 行 import 删除 `PADDED_MIN_MS`（`PROXY_WIDTH`、`SHORT_CLIP_MS` 已在导入中）

- [ ] **Step 4: 更新两个夹具文件**

`tests/fixtures/output_full/raw/unified-media.json` 与 `tests/fixtures/minimal/raw/unified-media.json`：
- `"schemaVersion": 2` → `"schemaVersion": 3`
- `"clipTransport": "videoDataURI"` → `"clipTransport": "mediaDataURI"`
- `"shortClipPolicy"` 改为 `{"minimumDurationMs": 2000, "imageProxyWidth": 720}`

（两个夹具的镜头均 ≥2s，clips[] 数组无需改动。）

- [ ] **Step 5: 全库清扫残留引用**

Run: `grep -rn "PADDED_MIN_MS\|videoDataURI\|recoveryMinimumDurationMs\|recoveryWidth" src/ tests/ schemas/`
Expected: 无输出（docs/ 由 Task 5 处理）

若有 `unavailable-m1` stub 相关测试断言旧值，一并更新。

- [ ] **Step 6: 运行相关测试**

Run: `.venv/bin/python -m pytest tests/unit/test_media_orchestrator.py tests/contract/test_cross_artifact.py -q`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add schemas/unified-media.json src/memoloupe/analysis/shot_pipeline.py src/memoloupe/analysis/media_orchestrator.py tests/fixtures tests/unit/test_media_orchestrator.py
git commit -m "feat(contract): unified-media schemaVersion 3 (mediaDataURI, image proxy policy)"
```

---

### Task 4: 集成测试适配（`test_media_evidence.py`）

**Files:**
- Modify: `tests/integration/test_media_evidence.py:148-197`

**Interfaces:**
- Consumes: Task 1 的 `build_clips` 新行为（1s 镜头 → jpg 代理）。
- Produces: 无新接口。

背景：`SHOTS_3X1S` 三个镜头均为 1s（<2000ms），改动后全部走图像代理；夹具视频 `three_color.mp4` 总长 3s（3 段 1s 颜色硬切 + 440Hz 音轨）。

- [ ] **Step 1: 重写两个 clip 构建测试并确认旧断言失败**

将 `test_build_clips_*`（约 145-197 行）整段替换为：

```python
def test_build_clips_short_shots_image_proxy(media_dir: Path, pool: FFmpegPool, tmp_path: Path) -> None:
    source = media_dir / "three_color.mp4"
    clips = build_clips(source, SHOTS_3X1S, True, DEFAULT_CONFIG, tmp_path, pool=pool)

    assert len(clips) == 3
    for clip, shot in zip(clips, SHOTS_3X1S):
        assert clip["shotID"] == shot["shotID"]
        assert clip["startMs"] == shot["finalStartMs"]
        assert clip["endMs"] == shot["finalEndMs"]
        assert clip["durationMs"] == 1000
        evidence = tmp_path / clip["file"]
        proxy = tmp_path / clip["modelFile"]
        assert evidence.is_file() and proxy.is_file()

        # 证据 clip 时长 1000±100ms（不受代理模态影响）
        info = _ffprobe_json(evidence, "-show_entries", "format=duration")
        duration_ms = float(info["format"]["duration"]) * 1000
        assert abs(duration_ms - 1000) <= 100

        # 短镜头（<2000ms）走中点帧图像代理
        assert clip["modelFile"].endswith(".jpg")
        assert clip["modelDurationMs"] == 1000
        norm = clip["modelNormalization"]
        assert norm["cacheKey"]
        assert norm["file"] == clip["modelFile"]
        assert norm["strategy"] == "frame-midpoint-w720"
        assert "padded" not in norm

        # 图像代理宽 720
        pinfo = _ffprobe_json(
            proxy, "-select_streams", "v:0", "-show_entries", "stream=width"
        )
        assert pinfo["streams"][0]["width"] == 720


def test_build_clips_long_shot_video_proxy(media_dir: Path, pool: FFmpegPool, tmp_path: Path) -> None:
    source = media_dir / "three_color.mp4"
    shots = [{"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 2500}]
    clips = build_clips(source, shots, True, DEFAULT_CONFIG, tmp_path, pool=pool)

    assert len(clips) == 1
    clip = clips[0]
    assert clip["modelFile"].endswith(".mp4")
    norm = clip["modelNormalization"]
    assert norm["strategy"] == "reencode-w720-fps10"
    assert "padded" not in norm

    # 视频代理：宽 720、fps 10、实测时长约 2500ms
    proxy = tmp_path / clip["modelFile"]
    pinfo = _ffprobe_json(
        proxy,
        "-select_streams", "v:0",
        "-show_entries", "stream=width,avg_frame_rate",
        "-show_entries", "format=duration",
    )
    stream = pinfo["streams"][0]
    assert stream["width"] == 720
    num, den = stream["avg_frame_rate"].split("/")
    assert float(num) / float(den) == pytest.approx(10.0)
    assert clip["modelDurationMs"] >= 2400
```

- [ ] **Step 2: 运行确认通过**

Run: `.venv/bin/python -m pytest tests/integration/test_media_evidence.py -q`
Expected: PASS

- [ ] **Step 3: 提交**

```bash
git add tests/integration/test_media_evidence.py
git commit -m "test(media): adapt build_clips integration tests to image proxy"
```

---

### Task 5: 文档同步

**Files:**
- Modify: `docs/03_PIPELINES_AND_ALGORITHMS.md`（约 193-196 行）
- Modify: `docs/07_SOURCE_DATA_CONTRACT.md`（534、541、615、622-625 行附近）
- Modify: `docs/02_DATA_AND_STATE_CONTRACTS.md`（258 行附近）
- Modify: `docs/06_DECISIONS_AND_ASSUMPTIONS.md`（新增 D-059）
- Modify: `docs/superpowers/specs/2026-09-03-short-clip-image-proxy-design.md`（补充 schema v3 修正记录，已在 brainstorm 阶段完成，无需再改）

- [ ] **Step 1: 更新 docs/03 §2.6 代理策略**

将约 193-196 行的补齐描述：

```
- 短于 800 ms 的 clip 可补齐；恢复策略可补到至少 2000 ms、宽度 720；
- 有音轨的短 clip 必须同时补帧与补静音，并截成音画等长的合法 MP4；
- ...
- 补齐只影响模型输入，不改变证据 clip 和镜头边界。
```

替换为：

```
- 短于 2000 ms 的镜头不生成视频代理，改用 final 区间中点帧 JPG
  （宽 720）作为图像代理；该阈值来自云端模型最短视频约束（D-058），
  图像化决策见 D-059；
- 2000 ms 及以上的镜头生成宽 720、fps 10 的重编码视频代理；
- 代理形态必须记录在 modelNormalization.strategy
  （frame-midpoint-w720 / reencode-w720-fps10）；
- 代理只影响模型输入，不改变证据 clip 和镜头边界。
```

- [ ] **Step 2: 更新 docs/07 契约条目**

- 534 行：`request.clipTransport` 的可选值 `videoDataURI` → `mediaDataURI`。
- 541 行：`request.shortClipPolicy` 描述"短 clip 补齐策略" → "短 clip 模态切换策略（图像代理阈值与宽度）"。
- 615 行示例：`"clipTransport": "videoDataURI"` → `"mediaDataURI"`。
- 622-625 行示例 shortClipPolicy → `"shortClipPolicy": { "minimumDurationMs": 2000, "imageProxyWidth": 720 }`。
- 若该文件记录了 unified-media 的 schemaVersion，同步为 3。

- [ ] **Step 3: 更新 docs/02**

258 行附近"默认 transport 为 `videoDataURI`" → "默认 transport 为 `mediaDataURI`（视频与短镜头图像代理混合）"。

- [ ] **Step 4: docs/06 新增 D-059**

在 D-058 之后追加：

```markdown
### D-059：短镜头模型代理改用中点帧图像（clips.v4，unified-media v3）

决策：`durationMs < 2000` 的镜头不再用 tpad/apad 补齐视频，改输出 final
区间中点帧 JPG（宽 720）作为模型代理；transport 按文件后缀选择
`image_url`/`video_url` part，对 qwen 与 mimo 统一生效。
`modelNormalization.strategy` 标记为 `frame-midpoint-w720`；图像代理的
`modelDurationMs` 取镜头真实 durationMs。unified-media schemaVersion
升 3：`clipTransport` 改为 `mediaDataURI`，`shortClipPolicy` 改为
`{minimumDurationMs, imageProxyWidth}`。v2 产物不再通过校验；output/
下均为开发样例，重跑即可，无迁移负担。

理由：补齐段是冻结画面+静音，对理解无信息量且可能误导；短镜头内容
变化小，单帧更具代表性，请求体从 MB 级视频降到 KB 级图像。
```

- [ ] **Step 5: 提交**

```bash
git add docs/03_PIPELINES_AND_ALGORITHMS.md docs/07_SOURCE_DATA_CONTRACT.md docs/02_DATA_AND_STATE_CONTRACTS.md docs/06_DECISIONS_AND_ASSUMPTIONS.md
git commit -m "docs: short-clip image proxy decision (D-059), contract v3 updates"
```

---

### Task 6: 全量回归 + 真实 key 冒烟

**Files:** 无（验证任务）

- [ ] **Step 1: 全量测试**

Run: `.venv/bin/python -m pytest -q`
Expected: 全部通过（此前基线 `1125 passed, 6 skipped`，本计划净增若干用例；数字以实际为准，关键是 0 failed）

- [ ] **Step 2: qwen 真实 key E2E 冒烟**

Run: `.venv/bin/python run_shot_analysis.py video/disney.MP4 --output-dir output/disney-qwen-img-20260903`
Expected: 退出码 0；`output/disney-qwen-img-20260903/raw/unified-media.json` 中短镜头 clip 的 `modelFile` 以 `.jpg` 结尾、`strategy` 为 `frame-midpoint-w720`，且三组分析 `status` 非 failed；`shot-analysis.html` 正常生成。

- [ ] **Step 3: mimo 真实 key 冒烟**

切换到 mimo provider 后对同一视频重跑到新输出目录，确认 `image_url` part 被 MiMo 接受（`unified-media.json` 中视觉组无 4xx 永久失败）。

- [ ] **Step 4: 清理旧代理缓存确认**

确认新输出目录 `clips/model-proxy/` 下短镜头为 `.jpg`、长镜头为 `.mp4`，无 tpad 产物（文件名与新策略一致即可）。

---

## Self-Review 记录

- Spec 覆盖：构建层（Task 1）、transport（Task 2）、schema/生产者/夹具（Task 3）、集成测试（Task 4）、文档含 D-059（Task 5）、回归与双 provider 冒烟（Task 6）——spec 各节均有对应任务。
- 占位符：无 TBD/TODO；所有代码步骤含完整代码与命令。
- 类型一致性：`image_proxy_file_rel`/`image_proxy_argv`/`model_normalization(kind=...)` 在 Task 1 定义、Task 4 消费；`ModelClip.is_image` 在 Task 2 定义并测试；`shortClipPolicy` 新形态在 Task 3 的 schema、生产者、夹具、断言间一致。
