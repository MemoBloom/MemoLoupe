"""Phase 1 镜头分析编排（docs/01 §9、docs/03 §1/§2.1/§5/§6/§7）。

执行模型：

- 串行执行固定步骤序列（顺序即 DAG，docs/03 §2.1）；每步先算语义指纹，
  :meth:`ArtifactStore.is_reusable` 命中且未被 ``--force``/``--no-cache``
  覆盖时复用，否则执行并经 ArtifactStore 原子写入。
- 指纹组成：源 revision + analyzedRange + 上游指纹 + 相关配置分组 +
  算法/实现版本（docs/03 §1）。不含 API key、输出路径、生成时间。
- 并发安全：output-dir 下的锁文件（``runtime.lockFileName``）记录
  pid/host/runID/startedAt；活锁（进程仍存在）直接失败且不写任何产物，
  陈旧锁接管，finally 释放。
- 能力降级（docs/03 §7）：ASR/UnifiedMLLM 未配置时写显式 skipped 产物；
  无音轨时音频检测写 unavailable；Apple Vision 不可用由
  analyze_camera_motion 内部降级为 capabilityStatus=unavailable。
  降级不致命，HTML 与校验照常进行。
- unified-media / asr 的产物状态为 partial/failed 时 manifest 不记
  complete，下次运行自动重跑（unified 经 checkpoints/ 断点续跑）。
- ``--align-shot-boundaries-to-audio``：detect_audio_cuts 排在
  extract_frames 之前；仅 synchronizedCut 且高置信的移动被采纳时才重写
  shots.json 的 final 边界并重算 durationMs（detected 边界永不修改），
  重写后 shots 指纹变化，下游镜头级步骤自然失效。
- 必需步骤（probe/shots/frames/clips/validate）失败立即终止并标 failed；
  其余步骤异常记 failed 继续，最终 status=partial；产物内嵌的
  skipped/unavailable/partial 降级状态不算步骤异常。
- 渲染后必须先校验再把阶段标为完成（docs/03 §2.13）。

非 artifact 步骤（build_clips 的 clip 列表、render、validate）的复用状态
记录在 output-dir 根的 ``.memoloupe-pipeline.json``（实现元数据，
与 manifest.json 同类，不属于业务契约）。
"""

from __future__ import annotations

import copy
import json
import os
import socket
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from memoloupe.analysis.asr_stage import ASR_STAGE_VERSION, run_asr_stage
from memoloupe.analysis.media_orchestrator import (
    UNIFIED_MEDIA_VERSION,
    build_skipped_unified_media,
    run_unified_media_analysis,
)
from memoloupe.analysis.vocabulary import load_vocabulary
from memoloupe.artifacts.schemas import ArtifactName
from memoloupe.artifacts.store import ArtifactStore, WriteMetadata
from memoloupe.core.atomic_io import write_json_atomic
from memoloupe.core.config import config_fingerprint, load_config
from memoloupe.core.hashing import content_revision_id, fingerprint
from memoloupe.core.logging import get_logger, log_step
from memoloupe.media.audio_cuts import AUDIO_CUTS_VERSION, detect_audio_cuts
from memoloupe.media.audio_energy import AUDIO_ENERGY_VERSION, detect_audio_energy
from memoloupe.media.audio_music import AUDIO_MUSIC_VERSION, detect_music
from memoloupe.media.clips import (
    CLIP_BUILD_VERSION,
    PADDED_MIN_MS,
    SHORT_CLIP_MS,
    build_clips,
)
from memoloupe.media.concurrency import FFmpegPool
from memoloupe.media.frames import FRAME_EXTRACTION_VERSION, extract_frames
from memoloupe.media.probe import probe_media
from memoloupe.media.quality import QUALITY_DETECTION_VERSION, detect_quality
from memoloupe.media.shots import SHOT_DETECTION_VERSION, detect_shots
from memoloupe.render.shot_html import SHOT_RENDER_VERSION, render_shot_html
from memoloupe.services.asr import build_asr_service
from memoloupe.services.mock import MockASRService, default_mock_unified
from memoloupe.services.unified_media import OpenAICompatibleUnifiedMedia
from memoloupe.validate.cross_artifact import validate_output_dir
from memoloupe.validate.html_contract import validate_html
from memoloupe.vision.apple_vision import (
    CAMERA_MOTION_VERSION,
    analyze_camera_motion,
)

#: 非 artifact 步骤的复用状态文件名（output-dir 根，实现元数据）。
PIPELINE_STATE_FILENAME = ".memoloupe-pipeline.json"

#: 必需步骤：失败立即终止整个阶段。
REQUIRED_STEPS = frozenset(
    {"probe_media", "detect_shots", "extract_frames", "build_clips", "validate"}
)

#: 05-04：可被 ``--skip`` 显式跳过的可选步骤（跳过写降级产物，不省略）。
#: extract_frames 虽属 REQUIRED_STEPS，但允许显式跳过（写 failed stub），
#: 此时"跳过"是用户意图而非失败。
SKIPPABLE_STEPS = frozenset(
    {
        "detect_audio_cuts",
        "run_asr",
        "detect_music",
        "extract_frames",
        "detect_audio_energy",
        "detect_quality",
        "unified_media_analysis",
        "analyze_camera_motion",
    }
)

#: 步骤执行顺序（docs/03 §2.1 DAG 的串行化）。
#: detect_audio_cuts 必须在 extract_frames 之前：--align 移动 final 边界后，
#: 帧/clip/能量/质量等镜头级证据都按新边界生成。
STEP_ORDER = (
    "acquire_lock",
    "probe_media",
    "detect_shots",
    "detect_audio_cuts",
    "run_asr",
    "detect_music",
    "extract_frames",
    "build_clips",
    "detect_audio_energy",
    "detect_quality",
    "unified_media_analysis",
    "analyze_camera_motion",
    "render_shot_html",
    "validate",
)


@dataclass
class StepRecord:
    """单个步骤的执行记录。"""

    name: str
    status: str  # complete/reused/skipped/unavailable/failed
    elapsed_ms: int
    detail: str | None = None


@dataclass
class PipelineReport:
    """阶段执行报告（docs/01 §9）。"""

    phase: str
    status: str  # complete/partial/failed
    steps: list[StepRecord]
    warnings: list[str]
    artifacts: list[str]  # 相对 output-dir 的路径
    elapsed_ms: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ShotAnalysisRequest:
    source: Path
    output_dir: Path
    analyzed_range: tuple[int, int] | None = None
    force_steps: frozenset[str] = frozenset()
    no_cache: bool = False
    config: dict | None = None  # None 时 load_config()
    align_boundaries: bool = False  # --align-shot-boundaries-to-audio
    mock_services: bool = False  # --mock-services：ASR/UnifiedMLLM 用可编程 mock
    # 显式注入的服务实例（测试/嵌入用）；None 时按 mock_services/config 构造。
    asr_service: Any = None
    unified_service: Any = None
    # 05-04：显式跳过的可选步骤（写 skipped/unavailable 降级产物，不省略）。
    skip_steps: frozenset[str] = frozenset()
    # 05-04：调试模式——只保留前 N 个镜头（产物不满足完整范围契约，见 warning）。
    max_shots: int | None = None


# ---------------------------------------------------------------------------
# stub 产物构建（M1 显式降级，全部 schema 合法）
# ---------------------------------------------------------------------------


def build_asr_stub() -> dict:
    """asr.json 降级产物：ASR 服务未配置。"""
    return {
        "service": "asr",
        "status": "skipped",
        "transcript": {"segments": []},
        "note": "ASR 服务未配置（M1）",
    }


def _truncate_shots(shots_doc: dict, max_shots: int) -> dict:
    """调试模式：只保留前 ``max_shots`` 个镜头（05-04 --max-shots）。

    返回裁剪后的副本；产物不满足完整范围契约（末镜头终点 ≠ analyzedRange
    终点、末 boundaryOut 非 sourceEnd），由调用方记 warning。
    """
    truncated = shots_doc["shots"][:max_shots]
    doc = dict(shots_doc)
    doc["shots"] = truncated
    analysis = dict(doc.get("analysis", {}))
    analysis["selectedBoundaryCount"] = max(0, len(truncated) - 1)
    analysis["note"] = "调试模式 --max-shots：仅保留前 N 个镜头，产物不满足完整范围契约"
    doc["analysis"] = analysis
    return doc


def build_frames_stub(shots: list[dict], revision_id: str) -> dict:
    """frame-evidence.json 降级产物：帧抽取被显式跳过。

    ``status=failed`` + ``frames=[]`` + failedFrames 全镜头——
    表达"未产出帧"，绝不伪装成已抽取。
    """
    return {
        "status": "failed",
        "request": {
            "sourceRevisionID": revision_id,
            "inputVideo": "（未抽取：extract_frames 被跳过）",
            "inputCacheKey": "skipped",
            "width": 640,
        },
        "extraction": {"mode": "skipped", "workerCount": 1, "cachedFrames": 0},
        "frames": [],
        "failedFrames": [
            {
                "shotID": shot["shotID"],
                "reason": "用户显式跳过 extract_frames（--skip）",
            }
            for shot in shots
        ],
        "note": "帧抽取未运行（--skip extract_frames）",
    }


def build_audio_energy_stub(shots: list[dict], media: dict, config: dict) -> dict:
    """audio-energy.json 降级产物：能量检测被显式跳过。

    ``hasAudio=false`` + 每镜头 ``label=unknown``/``medianDb=null``——
    D-007 语义：未测量 ≠ 静音，绝不伪造数值。
    """
    duration_ms = int(media.get("source", {}).get("durationMs", 0))
    return {
        "source": "skipped（--skip detect_audio_energy）",
        "durationMs": duration_ms,
        "sampleRate": 16000,
        "hasAudio": False,
        "thresholds": {},
        "shots": [
            {
                "shotID": shot["shotID"],
                "label": "unknown",
                "medianDb": None,
                "frameCount": 0,
                "note": "能量检测未运行（--skip）",
            }
            for shot in shots
        ],
        "note": "音频能量检测未运行（--skip detect_audio_energy）",
    }


def build_quality_stub(shots: list[dict], media: dict, config: dict) -> dict:
    """quality-flags.json 降级产物：质量检测被显式跳过。

    schema 只允许 ``status=complete``，因此用 ``confidence=unknown`` +
    空 ``flags`` 表达"未检测"——docs/02 §4.9 明确 confidence=unknown 时
    空 flags 不得解释为未发现问题。
    """
    return {
        "status": "complete",
        "method": "skipped（--skip detect_quality）",
        "audioStatus": "failed",
        "flaggedShotCount": 0,
        "shotCount": len(shots),
        "thresholds": {},
        "shots": [
            {
                "shotID": shot["shotID"],
                "startMs": int(shot["finalStartMs"]),
                "endMs": int(shot["finalEndMs"]),
                "flags": [],
                "confidence": "unknown",
                "measurements": {},
                "note": "质量检测未运行（--skip）",
            }
            for shot in shots
        ],
        "note": "质量检测未运行（--skip detect_quality）",
    }


def build_music_flags_stub(shots: list[dict], config: dict) -> dict:
    """music-flags.json 降级产物：BGM 检测未运行，全部镜头 unknown。"""
    entries = [
        {
            "shotID": shot["shotID"],
            "startMs": int(shot["finalStartMs"]),
            "endMs": int(shot["finalEndMs"]),
            "state": "unknown",
            "confidence": "unknown",
            "basis": "BGM 检测未运行（M1）",
            "musicOverlapRatio": 0.0,
            "silentOverlapRatio": 0.0,
            "events": [],
        }
        for shot in shots
    ]
    return {
        "status": "skipped",
        "method": "speechGapSpectralFlatness（M1 未运行）",
        "stateTally": {"music": 0, "silent": 0, "unknown": len(entries)},
        "thresholds": {},
        "speechGaps": [],
        "textureEvents": [],
        "musicIntervals": [],
        "shots": entries,
        "note": "BGM 检测未运行（M1）",
    }


def build_audio_cuts_stub(shots: list[dict], config: dict) -> dict:
    """audio-cuts.json 降级产物：音频切点检测未运行。

    首镜头 boundaryIn / 末镜头 boundaryOut 是确定性事实（sourceStart/
    sourceEnd），其余边界显式标 unavailable，不伪造音频对齐结论。
    """
    audio_cfg = config.get("audioCuts", {})
    last_index = len(shots) - 1
    entries: list[dict] = []
    for i, shot in enumerate(shots):
        if i == 0:
            boundary_in = {
                "classification": "sourceStart",
                "labelZh": "片头（音画同时开始）",
                "visualTimeMs": int(shot["finalStartMs"]),
                "confidence": "high",
            }
        else:
            boundary_in = {
                "classification": "unavailable",
                "labelZh": "音频切点检测未运行（M1）",
                "visualTimeMs": int(shot["finalStartMs"]),
                "confidence": "low",
            }
        if i == last_index:
            boundary_out = {
                "classification": "sourceEnd",
                "labelZh": "片尾（音画同时结束）",
                "visualTimeMs": int(shot["finalEndMs"]),
                "confidence": "high",
            }
        else:
            boundary_out = {
                "classification": "unavailable",
                "labelZh": "音频切点检测未运行（M1）",
                "visualTimeMs": int(shot["finalEndMs"]),
                "confidence": "low",
            }
        entries.append(
            {
                "shotID": shot["shotID"],
                "boundaryIn": boundary_in,
                "boundaryOut": boundary_out,
            }
        )
    return {
        "status": "unavailable",
        "analysis": {
            "method": "audioFeatureNoveltyHardCutCandidates",
            "analysisSampleRate": int(audio_cfg.get("analysisSampleRate", 16000)),
            "frameMs": int(audio_cfg.get("frameMs", 20)),
            "threshold": float(audio_cfg.get("threshold", 8.0)),
            "syncToleranceMs": int(audio_cfg.get("syncToleranceMs", 100)),
            "associationWindowMs": int(audio_cfg.get("associationWindowMs", 500)),
            "selectedBoundaryCount": 0,
            "note": "音频切点检测未运行（M1）",
        },
        "boundaries": [],
        "shots": entries,
    }


def build_camera_motion_stub(shots: list[dict], media: dict, config: dict) -> dict:
    """camera-motion.json 降级产物：Apple Vision helper 未接入。"""
    vision_cfg = config.get("vision", {})
    duration_ms = int(media.get("source", {}).get("durationMs", 0))
    entries = [
        {
            "shotID": shot["shotID"],
            "sequenceIndex": int(shot["sequenceIndex"]),
            "startMs": int(shot["finalStartMs"]),
            "endMs": int(shot["finalEndMs"]),
            "durationMs": int(shot["durationMs"]),
            "sampleCount": 0,
            "cameraMovement": "unknown",
            "cameraMovementCandidates": ["unknown"],
            "movementIntensity": "unknown",
            "confidence": "unknown",
            "neutralMotions": [],
            "needsReview": True,
            "metrics": {},
            "evidence": {
                "method": "unavailable（M1 未接 Apple Vision helper）",
                "discontinuityFrameIndexes": [],
                "frames": [],
            },
        }
        for shot in shots
    ]
    return {
        "analysis": {
            "method": "Apple Vision VNTrackHomographicImageRegistrationRequest + "
            "VNTrackOpticalFlowRequest",
            "capabilityStatus": "unavailable",
            "durationMs": duration_ms,
            "sampleFps": float(vision_cfg.get("sampleFps", 2.0)),
            "maximumFramesPerShot": int(vision_cfg.get("maximumFramesPerShot", 12)),
            "maximumImageDimension": int(vision_cfg.get("maximumImageDimension", 960)),
            "note": "M1 未接 Apple Vision helper",
        },
        "shots": entries,
    }


def unified_stub_schema_fingerprint() -> str:
    """unified-media v2 stub 指纹（无真实模型时仍必须随契约失效）。"""
    return fingerprint(
        {
            "prompt": "none",
            "schema": "unified-media.v2",
            "vocab": 3,
            "parser": "groups.v2",
        }
    )


def build_unified_media_stub(clips: list[dict], media: dict, config: dict) -> dict:
    """unified-media.json 降级产物：模型未运行，全部镜头 pending。"""
    model_cfg = config.get("unifiedModel", {})
    shot_statuses = {clip["shotID"]: "pending" for clip in clips}
    return {
        "schemaVersion": 2,
        "service": "unifiedAudioVideo",
        "schemaFingerprint": unified_stub_schema_fingerprint(),
        "request": {
            "model": "unavailable-m1",
            "fallbackModel": None,
            "clipTransport": "videoDataURI",
            "batchSize": int(model_cfg.get("batchSize", 4)),
            "concurrency": int(model_cfg.get("concurrency", 10)),
            "externalFrameExtraction": False,
            "videoFPS": float(model_cfg.get("videoFPS", 10.0)),
            "mediaResolution": str(model_cfg.get("mediaResolution", "default")),
            "sourceRevisionID": media.get("source", {}).get("revisionID"),
            "shortClipPolicy": {
                "minimumDurationMs": SHORT_CLIP_MS,
                "recoveryMinimumDurationMs": PADDED_MIN_MS,
                "recoveryWidth": 720,
            },
        },
        "retryPolicy": {
            "maxRetries": int(model_cfg.get("maxRetries", 3)),
            "fallbackFromBatchToSingleShot": True,
            "checkpointAfterEachRequest": True,
        },
        "clips": clips,
        "batches": [],
        "shotStatuses": shot_statuses,
        "completedShots": 0,
        "failedShots": 0,
        "pendingShots": len(shot_statuses),
        "permanentFailureShots": 0,
        "terminal": False,
        "status": "skipped",
    }


# ---------------------------------------------------------------------------
# 服务构造与音频边界对齐
# ---------------------------------------------------------------------------


def _service_marker(injected: Any, mock_services: bool, service: Any) -> str:
    """服务来源标记（进入指纹；不含密钥等敏感信息）。"""
    if injected is not None:
        return "injected"
    if mock_services:
        return "mock"
    return "configured" if service is not None else "none"


def _build_asr_service(config: dict, mock_services: bool) -> Any:
    """按配置构造 ASR 服务；未配置时返回 None（调用方走 skipped 降级）。

    05-01C：provider 选择（openai-json / openai-multipart）在
    :func:`memoloupe.services.asr.build_asr_service` 统一处理。
    """
    if mock_services:
        return MockASRService()
    return build_asr_service(config)


def _build_unified_service(
    config: dict, mock_services: bool, shot_ids: list[str]
) -> Any:
    """按配置构造 UnifiedMLLM 服务；未配置时返回 None（skipped 降级）。"""
    if mock_services:
        return default_mock_unified(shot_ids)
    model_cfg = config.get("unifiedModel", {})
    api_key = model_cfg.get("apiKey")
    base_url = model_cfg.get("baseUrl")
    model = model_cfg.get("model")
    if not (api_key and base_url and model):
        return None
    fallback = model_cfg.get("fallbackModel")
    return OpenAICompatibleUnifiedMedia(
        base_url=str(base_url),
        api_key=str(api_key),
        model=str(model),
        fallback_model=str(fallback) if isinstance(fallback, str) and fallback else None,
        timeout_sec=float(model_cfg.get("timeoutSec", 300.0)),
        video_fps=float(model_cfg.get("videoFPS", 10.0)),
        media_resolution=str(model_cfg.get("mediaResolution", "default")),
        max_completion_tokens=int(model_cfg.get("maxCompletionTokens", 4096)),
        thinking_mode=str(model_cfg.get("thinkingMode", "disabled")),
    )


def _apply_moved_boundaries(shots_doc: dict, moved: list[dict]) -> dict | None:
    """把音频对齐移动计划应用到 shots.json 的 final 边界（docs/03 §2.4）。

    - detected 边界永不修改；相邻镜头共享边界一起移动并重算 durationMs；
    - 幂等守卫：所有待移动边界当前的 final 值必须仍等于 visualTimeMs，
      否则（已应用过或数据不一致）返回 None，不重写。
    """
    shots = shots_doc.get("shots")
    if not isinstance(shots, list):
        return None
    by_id = {s.get("shotID"): s for s in shots if isinstance(s, dict)}
    for move in moved:
        if not isinstance(move, dict):
            return None
        left = by_id.get(move.get("leftShotID"))
        right = by_id.get(move.get("rightShotID"))
        visual = move.get("visualTimeMs")
        audio = move.get("audioTimeMs")
        if not (
            isinstance(left, dict)
            and isinstance(right, dict)
            and isinstance(visual, int)
            and isinstance(audio, int)
        ):
            return None
        if left.get("finalEndMs") != visual or right.get("finalStartMs") != visual:
            return None
    aligned = copy.deepcopy(shots_doc)
    aligned_by_id = {s.get("shotID"): s for s in aligned["shots"] if isinstance(s, dict)}
    for move in moved:
        left = aligned_by_id[move["leftShotID"]]
        right = aligned_by_id[move["rightShotID"]]
        audio_time = int(move["audioTimeMs"])
        left["finalEndMs"] = audio_time
        left["durationMs"] = audio_time - int(left["finalStartMs"])
        right["finalStartMs"] = audio_time
        right["durationMs"] = int(right["finalEndMs"]) - audio_time
    return aligned


# ---------------------------------------------------------------------------
# 锁与流水线状态文件
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但属于其他用户
    except OSError:
        return False
    return True


class _Lock:
    """output-dir 写锁（docs/03 §6）。"""

    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.acquired = False
        self.took_over_stale: dict[str, Any] | None = None

    def acquire(self) -> str | None:
        """获取锁；冲突时返回警告文案，成功返回 None。陈旧锁接管并记录。"""
        if self.path.exists():
            holder: dict[str, Any] = {}
            try:
                holder = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                holder = {}
            pid = holder.get("pid")
            if isinstance(pid, int) and pid > 0 and _pid_alive(pid):
                return (
                    f"output-dir 已被进行中的分析锁定：pid={pid} "
                    f"host={holder.get('host')} runID={holder.get('runID')} "
                    f"startedAt={holder.get('startedAt')}"
                )
            self.took_over_stale = holder
        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "runID": self.run_id,
            "startedAt": _utc_now_iso(),
        }
        write_json_atomic(self.path, payload)
        self.acquired = True
        return None

    def release(self) -> None:
        """只释放自己持有的锁（runID 匹配），绝不误删他人锁。"""
        if not self.acquired:
            return
        try:
            holder = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if holder.get("runID") == self.run_id and holder.get("pid") == os.getpid():
            try:
                self.path.unlink()
            except OSError:
                pass
        self.acquired = False


def _load_pipeline_state(root: Path) -> dict:
    path = root / PIPELINE_STATE_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "steps": {}}
    if not isinstance(data, dict) or not isinstance(data.get("steps"), dict):
        return {"version": 1, "steps": {}}
    return data


def _write_pipeline_state(root: Path, state: dict) -> None:
    write_json_atomic(root / PIPELINE_STATE_FILENAME, state)


def _tool_versions(config: dict) -> dict[str, str]:
    """ffmpeg/ffprobe 版本首行，用于 probe 指纹（工具版本变化触发重探）。"""
    from memoloupe.media.proc import run_process

    ffmpeg_cfg = config.get("ffmpeg", {})
    timeout = float(ffmpeg_cfg.get("probeTimeoutSec", 30.0))
    versions: dict[str, str] = {}
    for key, binary in (
        ("ffmpeg", str(ffmpeg_cfg.get("ffmpegPath", "ffmpeg"))),
        ("ffprobe", str(ffmpeg_cfg.get("ffprobePath", "ffprobe"))),
    ):
        result = run_process([binary, "-version"], timeout_sec=timeout)
        first_line = result.stdout.decode("utf-8", errors="replace").splitlines()
        versions[key] = first_line[0] if first_line else "unknown"
    return versions


# ---------------------------------------------------------------------------
# 编排器
# ---------------------------------------------------------------------------


class ShotAnalysisPipeline:
    """Phase 1 阶段编排器（可注入依赖的普通对象，docs/01 §9）。"""

    def run(self, request: ShotAnalysisRequest) -> PipelineReport:
        started = time.monotonic()
        run_id = uuid.uuid4().hex[:8]
        logger = get_logger(
            "memoloupe.analysis.shot_pipeline", run_id=run_id, phase="shot"
        )
        config = request.config if request.config is not None else load_config()
        source = Path(request.source)
        out_dir = Path(request.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        store = ArtifactStore(out_dir)
        pool = FFmpegPool(int(config["ffmpeg"]["globalConcurrency"]))
        state = _load_pipeline_state(out_dir)

        steps: list[StepRecord] = []
        warnings: list[str] = []
        hard_failure = False
        any_failed = False
        # 本次运行是否有产物步骤真正执行（render/validate 复用的前置条件）
        any_produced = False

        lock = _Lock(
            out_dir / str(config["runtime"].get("lockFileName", ".memoloupe.lock")),
            run_id,
        )

        def record(name: str, status: str, elapsed_ms: int, detail: str | None = None) -> None:
            log_step(logger, name, status, elapsed_ms, **({"detail": detail} if detail else {}))
            steps.append(StepRecord(name=name, status=status, elapsed_ms=elapsed_ms, detail=detail))

        def cacheable(name: str) -> bool:
            return not request.no_cache and name not in request.force_steps

        def run_artifact_step(
            name: str,
            artifact: ArtifactName,
            fp: str,
            fn: Callable[[], dict],
            status_of: Callable[[dict], str | None] | None = None,
            skip_fn: Callable[[], dict] | None = None,
        ) -> dict | None:
            """通用 artifact 步骤：复用判定 → 执行 → 原子写入。失败按必需性处理。

            ``status_of`` 从产物内嵌状态（如 unified-media.status、
            camera-motion.analysis.capabilityStatus）提取步骤语义状态：
            - partial/failed 时 manifest 不记 complete，下次运行自动重跑
              （断点续跑），并记 warning、pipeline 落 partial；
            - skipped/unavailable 是确定性降级，manifest 记 complete
              （下次可直接复用），记 warning 但不影响整体 complete。

            ``skip_fn``：步骤在 ``request.skip_steps`` 中时生成显式降级
            stub（05-04：跳过 ≠ absent，产物保持合法且状态可见）。
            """
            nonlocal hard_failure, any_failed, any_produced
            step_start = time.monotonic()
            if name in request.skip_steps:
                if skip_fn is None:
                    record(name, "failed", _elapsed(step_start),
                           detail=f"步骤 {name} 被 --skip 但无降级实现")
                    warnings.append(f"步骤 {name} 被 --skip 但无降级实现")
                    return None
                data = skip_fn()
                doc_status = status_of(data) if status_of is not None else None
                manifest_status = (
                    doc_status if doc_status in ("partial", "failed") else "complete"
                )
                store.write(artifact, data, WriteMetadata(fingerprint=fp, status=manifest_status))
                any_produced = True
                record(name, "skipped", 0, detail="用户显式跳过（--skip）")
                warnings.append(f"步骤 {name} 已被 --skip 显式跳过（写降级产物）")
                return data
            if cacheable(name) and store.is_reusable(artifact, fp):
                record(name, "reused", _elapsed(step_start), detail=f"fingerprint={fp}")
                return store.read(artifact)
            try:
                data = fn()
                doc_status = status_of(data) if status_of is not None else None
                manifest_status = (
                    doc_status if doc_status in ("partial", "failed") else "complete"
                )
                store.write(artifact, data, WriteMetadata(fingerprint=fp, status=manifest_status))
            except Exception as exc:
                elapsed = _elapsed(step_start)
                record(name, "failed", elapsed, detail=str(exc))
                warnings.append(f"步骤 {name} 失败：{exc}")
                if name in REQUIRED_STEPS:
                    hard_failure = True
                else:
                    any_failed = True
                return None
            any_produced = True
            if doc_status is None or doc_status == "complete":
                record(name, "complete", _elapsed(step_start))
            else:
                record(name, doc_status, _elapsed(step_start))
                if doc_status in ("partial", "failed"):
                    any_failed = True
                    warnings.append(f"步骤 {name} 未完成：产物状态 {doc_status}")
                else:
                    warnings.append(f"步骤 {name} 显式降级：产物状态 {doc_status}")
            return data

        try:
            # 1. acquire_lock -------------------------------------------------
            step_start = time.monotonic()
            conflict = lock.acquire()
            if conflict is not None:
                record("acquire_lock", "failed", _elapsed(step_start), detail=conflict)
                warnings.append(conflict)
                return self._report(steps, warnings, out_dir, started, "failed")
            if lock.took_over_stale is not None:
                stale = lock.took_over_stale
                warnings.append(
                    f"接管陈旧锁（进程已退出）：pid={stale.get('pid')} "
                    f"runID={stale.get('runID')}"
                )
            record("acquire_lock", "complete", _elapsed(step_start), detail=f"runID={run_id}")

            # 2. probe_media --------------------------------------------------
            revision_id = content_revision_id(source)
            analyzed = (
                {"startMs": request.analyzed_range[0], "endMs": request.analyzed_range[1]}
                if request.analyzed_range
                else None
            )
            media_fp = fingerprint(
                {
                    "artifact": "media",
                    "sourceRevision": revision_id,
                    "analyzedRange": analyzed,
                    "tools": _tool_versions(config),
                    "config": config_fingerprint(config, ["ffmpeg"]),
                }
            )
            media = run_artifact_step(
                "probe_media",
                ArtifactName.MEDIA,
                media_fp,
                lambda: probe_media(
                    source, config, pool=pool, analyzed_range=request.analyzed_range
                ),
            )
            if hard_failure or media is None:
                return self._report(steps, warnings, out_dir, started, "failed")

            # 3. detect_shots（--align 时已对齐的 shots.json 也算可复用）--------
            shots_fp = fingerprint(
                {
                    "artifact": "shots",
                    "media": media_fp,
                    "config": config_fingerprint(config, ["shots"]),
                    "version": SHOT_DETECTION_VERSION,
                }
            )
            # audio-cuts 指纹预先计算：既用于该步骤本身，也用于对齐后
            # shots.json 的派生指纹（内容可寻址，重跑时稳定）。
            has_audio = bool(media["source"].get("audioTracks"))
            audio_cuts_fp = fingerprint(
                {
                    "artifact": "audio-cuts",
                    "shots": shots_fp,
                    "hasAudio": has_audio,
                    "align": request.align_boundaries,
                    "config": config_fingerprint(config, ["audioCuts"]),
                    "version": AUDIO_CUTS_VERSION,
                }
            )
            aligned_shots_fp = (
                fingerprint(
                    {
                        "artifact": "shots",
                        "base": shots_fp,
                        "alignedWith": audio_cuts_fp,
                    }
                )
                if request.align_boundaries
                else None
            )
            shots_doc: dict | None = None
            shots_fp_eff = shots_fp
            step_start = time.monotonic()
            reused_shots = False
            if cacheable("detect_shots"):
                if store.is_reusable(ArtifactName.SHOTS, shots_fp):
                    reused_shots = True
                elif aligned_shots_fp is not None and store.is_reusable(
                    ArtifactName.SHOTS, aligned_shots_fp
                ):
                    reused_shots = True
                    shots_fp_eff = aligned_shots_fp
            if reused_shots:
                record("detect_shots", "reused", _elapsed(step_start))
                shots_doc = store.read(ArtifactName.SHOTS)
            else:
                try:
                    shots_doc = detect_shots(source, media, config, pool=pool)
                    store.write(
                        ArtifactName.SHOTS, shots_doc, WriteMetadata(fingerprint=shots_fp)
                    )
                except Exception as exc:
                    record("detect_shots", "failed", _elapsed(step_start), detail=str(exc))
                    warnings.append(f"步骤 detect_shots 失败：{exc}")
                    return self._report(steps, warnings, out_dir, started, "failed")
                any_produced = True
                record("detect_shots", "complete", _elapsed(step_start))
            if shots_doc is None:
                return self._report(steps, warnings, out_dir, started, "failed")
            shots: list[dict] = shots_doc["shots"]
            # 05-04：调试模式 --max-shots——只保留前 N 个镜头。
            if request.max_shots is not None and len(shots) > request.max_shots:
                shots_doc = _truncate_shots(shots_doc, request.max_shots)
                store.write(
                    ArtifactName.SHOTS, shots_doc,
                    WriteMetadata(fingerprint=shots_fp_eff),
                )
                shots = shots_doc["shots"]
                any_produced = True
                warnings.append(
                    f"调试模式 --max-shots={request.max_shots}：仅保留前 "
                    f"{len(shots)} 个镜头；产物不满足完整范围契约，"
                    "validate 预期报错"
                )

            # 4. detect_audio_cuts（必须在 extract_frames 之前：--align 可能改
            #    final 边界，帧/clip/能量/质量都按新边界生成）--------------------
            audio_cuts_doc = run_artifact_step(
                "detect_audio_cuts",
                ArtifactName.AUDIO_CUTS,
                audio_cuts_fp,
                lambda: detect_audio_cuts(
                    source,
                    shots_doc,
                    media,
                    config,
                    pool=pool,
                    align_boundaries=request.align_boundaries,
                ),
                status_of=lambda d: d.get("status"),
                skip_fn=lambda: build_audio_cuts_stub(shots, config),
            )

            # 4a. --align：仅当检测给出移动计划且 shots 仍是检测原值时才重写
            #     shots.json（幂等守卫在 _apply_moved_boundaries 内）。
            if (
                request.align_boundaries
                and audio_cuts_doc is not None
                and shots_fp_eff == shots_fp
            ):
                moved = audio_cuts_doc.get("movedBoundaries")
                if isinstance(moved, list) and moved:
                    aligned_doc = _apply_moved_boundaries(shots_doc, moved)
                    if aligned_doc is not None:
                        assert aligned_shots_fp is not None
                        store.write(
                            ArtifactName.SHOTS,
                            aligned_doc,
                            WriteMetadata(fingerprint=aligned_shots_fp),
                        )
                        shots_doc = aligned_doc
                        shots = shots_doc["shots"]
                        shots_fp_eff = aligned_shots_fp
                        any_produced = True
                        warnings.append(
                            f"音频对齐：{len(moved)} 个 final 边界已移动到音频切点"
                            "（detected 边界保持不变）"
                        )

            # 5. run_asr（链 media 指纹；服务不可用 → skipped 降级产物）---------
            asr_service = request.asr_service
            if asr_service is None:
                asr_service = _build_asr_service(config, request.mock_services)
            asr_marker = _service_marker(
                request.asr_service, request.mock_services, asr_service
            )
            asr_fp = fingerprint(
                {
                    "artifact": "asr",
                    "media": media_fp,
                    "config": config_fingerprint(config, ["asr"]),
                    "service": asr_marker,
                    "version": ASR_STAGE_VERSION,
                }
            )
            asr_doc = run_artifact_step(
                "run_asr",
                ArtifactName.ASR,
                asr_fp,
                lambda: run_asr_stage(source, media, config, service=asr_service),
                status_of=lambda d: d.get("status"),
                skip_fn=lambda: build_asr_stub(),
            )

            # 6. detect_music（全轨纹理始终运行；ASR complete 时再融合
            #    语音间隙锚点，ASR 非 complete 时降低置信度）--------------------
            music_fp = fingerprint(
                {
                    "artifact": "music-flags",
                    "shots": shots_fp_eff,
                    "asr": asr_fp,
                    "hasAudio": has_audio,
                    "config": config_fingerprint(config, ["music"]),
                    "version": AUDIO_MUSIC_VERSION,
                }
            )
            run_artifact_step(
                "detect_music",
                ArtifactName.MUSIC_FLAGS,
                music_fp,
                lambda: detect_music(source, shots, asr_doc, media, config, pool=pool),
                status_of=lambda d: d.get("status"),
                skip_fn=lambda: build_music_flags_stub(shots, config),
            )

            # 7. extract_frames -----------------------------------------------
            frames_fp = fingerprint(
                {
                    "artifact": "frame-evidence",
                    "shots": shots_fp_eff,
                    "version": FRAME_EXTRACTION_VERSION,
                }
            )
            frames = run_artifact_step(
                "extract_frames",
                ArtifactName.FRAME_EVIDENCE,
                frames_fp,
                lambda: extract_frames(source, shots, media, config, out_dir, pool=pool),
                skip_fn=lambda: build_frames_stub(shots, revision_id),
            )
            if hard_failure:
                return self._report(steps, warnings, out_dir, started, "failed")

            # 8. build_clips ---------------------------------------------------
            clips_fp = fingerprint(
                {
                    "artifact": "clips",
                    "shots": shots_fp_eff,
                    "hasAudio": has_audio,
                    "version": CLIP_BUILD_VERSION,
                }
            )
            clips = self._clips_step(
                request=request,
                store=store,
                state=state,
                out_dir=out_dir,
                clips_fp=clips_fp,
                produce=lambda: build_clips(
                    source, shots, has_audio, config, out_dir, pool=pool
                ),
                cacheable=cacheable("build_clips"),
                record=record,
            )
            if clips is None:
                # build_clips 是必需步骤
                return self._report(steps, warnings, out_dir, started, "failed")
            if steps[-1].status == "complete":
                any_produced = True

            # 9. detect_audio_energy -------------------------------------------
            energy_fp = fingerprint(
                {
                    "artifact": "audio-energy",
                    "shots": shots_fp_eff,
                    "hasAudio": has_audio,
                    "config": config_fingerprint(config, ["audioEnergy"]),
                    "version": AUDIO_ENERGY_VERSION,
                }
            )
            run_artifact_step(
                "detect_audio_energy",
                ArtifactName.AUDIO_ENERGY,
                energy_fp,
                lambda: detect_audio_energy(source, shots, has_audio, config, pool=pool),
                skip_fn=lambda: build_audio_energy_stub(shots, media, config),
            )

            # 10. detect_quality ------------------------------------------------
            quality_fp = fingerprint(
                {
                    "artifact": "quality-flags",
                    "shots": shots_fp_eff,
                    "hasAudio": has_audio,
                    "config": config_fingerprint(config, ["quality"]),
                    "version": QUALITY_DETECTION_VERSION,
                }
            )
            run_artifact_step(
                "detect_quality",
                ArtifactName.QUALITY_FLAGS,
                quality_fp,
                lambda: detect_quality(source, shots, has_audio, config, pool=pool),
                skip_fn=lambda: build_quality_stub(shots, media, config),
            )

            # 11. unified_media_analysis（链 clips 指纹 + 词表版本；服务未配置
            #     → skipped 降级产物；partial/failed 下次自动断点续跑）----------
            vocab = load_vocabulary()
            unified_service = request.unified_service
            if unified_service is None:
                unified_service = _build_unified_service(
                    config, request.mock_services, [str(s["shotID"]) for s in shots]
                )
            unified_marker = _service_marker(
                request.unified_service, request.mock_services, unified_service
            )
            unified_fp = fingerprint(
                {
                    "artifact": "unified-media",
                    "clips": clips_fp,
                    "config": config_fingerprint(config, ["unifiedModel"]),
                    "service": unified_marker,
                    "version": UNIFIED_MEDIA_VERSION,
                    "vocab": vocab.version,
                }
            )

            def produce_unified() -> dict:
                if unified_service is None:
                    return build_skipped_unified_media(clips, config, revision_id)
                return run_unified_media_analysis(
                    store,
                    clips,
                    unified_service,
                    config=config,
                    vocab=vocab,
                    source_revision=revision_id,
                )

            run_artifact_step(
                "unified_media_analysis",
                ArtifactName.UNIFIED_MEDIA,
                unified_fp,
                produce_unified,
                status_of=lambda d: d.get("status"),
                skip_fn=lambda: build_skipped_unified_media(clips, config, revision_id),
            )

            # 12. analyze_camera_motion（链 shots 指纹；helper 不可用由适配器
            #     内部降级为 capabilityStatus=unavailable）----------------------
            camera_fp = fingerprint(
                {
                    "artifact": "camera-motion",
                    "shots": shots_fp_eff,
                    "config": config_fingerprint(config, ["vision"]),
                    "version": CAMERA_MOTION_VERSION,
                }
            )
            run_artifact_step(
                "analyze_camera_motion",
                ArtifactName.CAMERA_MOTION,
                camera_fp,
                lambda: analyze_camera_motion(source, shots, media, config, pool=pool),
                status_of=lambda d: (d.get("analysis") or {}).get("capabilityStatus"),
                skip_fn=lambda: build_camera_motion_stub(shots, media, config),
            )

            artifact_fps = {
                "media": media_fp,
                "shots": shots_fp_eff,
                "audio-cuts": audio_cuts_fp,
                "asr": asr_fp,
                "music-flags": music_fp,
                "frame-evidence": frames_fp,
                "clips": clips_fp,
                "audio-energy": energy_fp,
                "quality-flags": quality_fp,
                "unified-media": unified_fp,
                "camera-motion": camera_fp,
            }

            # 9. render_shot_html -----------------------------------------------
            render_fp = fingerprint(
                {
                    "step": "render_shot_html",
                    "artifacts": artifact_fps,
                    "version": SHOT_RENDER_VERSION,
                    "documentStatus": "draft",
                }
            )
            render_reused = (
                cacheable("render_shot_html")
                and not any_produced
                and state["steps"].get("render_shot_html", {}).get("fingerprint") == render_fp
                and (out_dir / "shot-analysis.html").is_file()
            )
            step_start = time.monotonic()
            if render_reused:
                record("render_shot_html", "reused", _elapsed(step_start))
            else:
                try:
                    render_shot_html(out_dir, status="draft")
                except Exception as exc:
                    record("render_shot_html", "failed", _elapsed(step_start), detail=str(exc))
                    any_failed = True
                    warnings.append(f"步骤 render_shot_html 失败：{exc}")
                else:
                    any_produced = True
                    record("render_shot_html", "complete", _elapsed(step_start))
                    state["steps"]["render_shot_html"] = {"fingerprint": render_fp}
                    _write_pipeline_state(out_dir, state)

            # 10. validate（渲染后必须先校验再标完成，docs/03 §2.13）------------
            validate_fp = fingerprint(
                {
                    "step": "validate",
                    "artifacts": artifact_fps,
                    "strict": True,
                    "html": (out_dir / "shot-analysis.html").is_file(),
                }
            )
            validate_reused = (
                cacheable("validate")
                and not any_produced
                and state["steps"].get("validate", {}).get("fingerprint") == validate_fp
            )
            step_start = time.monotonic()
            if validate_reused:
                record("validate", "reused", _elapsed(step_start))
            else:
                issues = list(validate_output_dir(out_dir, strict=True))
                html_path = out_dir / "shot-analysis.html"
                if html_path.is_file():
                    issues.extend(validate_html(html_path, root=out_dir, strict=True))
                errors = [i for i in issues if i.severity == "error"]
                for issue in issues:
                    if issue.severity == "warning":
                        warnings.append(
                            f"校验警告 {issue.artifact}:{issue.json_path} {issue.message}"
                        )
                if errors:
                    detail = "; ".join(
                        f"{i.artifact}:{i.json_path} {i.message}" for i in errors[:5]
                    )
                    if request.max_shots is not None:
                        # 05-04：调试模式（--max-shots 截断产物）下 validate
                        # 降级为 warning，不阻断阶段（产物本就不满足完整契约）。
                        record("validate", "skipped", _elapsed(step_start),
                               detail="--max-shots 调试模式：校验错误降级为警告")
                        for issue in errors:
                            warnings.append(
                                f"校验错误（调试模式降级）{issue.artifact}:"
                                f"{issue.json_path} {issue.message}"
                            )
                    else:
                        record("validate", "failed", _elapsed(step_start), detail=detail)
                        hard_failure = True
                else:
                    record(
                        "validate",
                        "complete",
                        _elapsed(step_start),
                        detail=f"{len(errors)} 错误 / "
                        f"{sum(1 for i in issues if i.severity == 'warning')} 警告",
                    )
                    state["steps"]["validate"] = {"fingerprint": validate_fp}
                    _write_pipeline_state(out_dir, state)
        finally:
            lock.release()

        if hard_failure:
            status = "failed"
        elif any_failed:
            status = "partial"
        else:
            status = "complete"
        return self._report(steps, warnings, out_dir, started, status)

    # ------------------------------------------------------------------
    # 子步骤
    # ------------------------------------------------------------------

    def _clips_step(
        self,
        *,
        request: ShotAnalysisRequest,
        store: ArtifactStore,
        state: dict,
        out_dir: Path,
        clips_fp: str,
        produce: Callable[[], list[dict]],
        cacheable: bool,
        record: Callable[..., None],
    ) -> list[dict] | None:
        """build_clips：clip 列表不是独立 artifact，复用状态记录在流水线状态文件。

        复用条件：状态指纹匹配 + unified-media.json 存在且其 clips 文件都在。
        """
        step_start = time.monotonic()
        if cacheable and state["steps"].get("build_clips", {}).get("fingerprint") == clips_fp:
            try:
                unified = store.read(ArtifactName.UNIFIED_MEDIA)
            except Exception:
                unified = None
            if unified is not None:
                clips = unified.get("clips")
                if isinstance(clips, list) and all(
                    isinstance(c, dict)
                    and isinstance(c.get("file"), str)
                    and (out_dir / c["file"]).is_file()
                    for c in clips
                ):
                    record("build_clips", "reused", _elapsed(step_start))
                    return clips
        try:
            clips = produce()
        except Exception as exc:
            record("build_clips", "failed", _elapsed(step_start), detail=str(exc))
            return None
        state["steps"]["build_clips"] = {"fingerprint": clips_fp}
        _write_pipeline_state(out_dir, state)
        record("build_clips", "complete", _elapsed(step_start), detail=f"{len(clips)} clips")
        return clips

    # ------------------------------------------------------------------

    def _report(
        self,
        steps: list[StepRecord],
        warnings: list[str],
        out_dir: Path,
        started: float,
        status: str,
    ) -> PipelineReport:
        return PipelineReport(
            phase="shot",
            status=status,
            steps=steps,
            warnings=warnings,
            artifacts=_collect_artifacts(out_dir),
            elapsed_ms=_elapsed(started),
        )


def _elapsed(start: float) -> int:
    return round((time.monotonic() - start) * 1000)


def _collect_artifacts(out_dir: Path) -> list[str]:
    """收集 output-dir 中存在的 Phase 1 产物相对路径（稳定排序）。"""
    candidates = [
        f"raw/{name.value}.json"
        for name in ArtifactName
        if name not in (ArtifactName.STORY_BLOCKS, ArtifactName.STYLE_PROFILE)
    ]
    candidates.append("shot-analysis.html")
    return sorted(rel for rel in candidates if (out_dir / rel).is_file())
