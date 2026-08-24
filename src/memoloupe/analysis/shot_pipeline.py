"""Phase 1 镜头分析编排（docs/01 §9、docs/03 §1/§2.1/§5/§6/§7）。

执行模型：

- 串行执行固定步骤序列（顺序即 DAG）；每步先算语义指纹，
  :meth:`ArtifactStore.is_reusable` 命中且未被 ``--force``/``--no-cache``
  覆盖时复用，否则执行并经 ArtifactStore 原子写入。
- 指纹组成：源 revision + analyzedRange + 上游指纹 + 相关配置分组 +
  算法/实现版本（docs/03 §1）。不含 API key、输出路径、生成时间。
- 并发安全：output-dir 下的锁文件（``runtime.lockFileName``）记录
  pid/host/runID/startedAt；活锁（进程仍存在）直接失败且不写任何产物，
  陈旧锁接管，finally 释放。
- 降级：M1 未接 ASR / BGM / 音频切点 / Apple Vision / UnifiedMLLM，
  ``stub_unavailable`` 步骤写入 5 个显式降级产物（schema 合法、
  状态 skipped/unavailable），HTML 与校验照常进行（docs/03 §7）。
- 必需步骤（probe/shots/frames/clips/validate）失败立即终止并标 failed；
  其余步骤失败记 failed 继续，最终 status=partial。
- 渲染后必须先校验再把阶段标为完成（docs/03 §2.13）。

非 artifact 步骤（build_clips 的 clip 列表、render、validate）的复用状态
记录在 output-dir 根的 ``.memoloupe-pipeline.json``（实现元数据，
与 manifest.json 同类，不属于业务契约）。
"""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from memoloupe.artifacts.schemas import ArtifactName
from memoloupe.artifacts.store import ArtifactStore, WriteMetadata
from memoloupe.core.atomic_io import write_json_atomic
from memoloupe.core.config import config_fingerprint, load_config
from memoloupe.core.hashing import content_revision_id, fingerprint
from memoloupe.core.logging import get_logger, log_step
from memoloupe.media.audio_energy import AUDIO_ENERGY_VERSION, detect_audio_energy
from memoloupe.media.clips import CLIP_BUILD_VERSION, build_clips
from memoloupe.media.concurrency import FFmpegPool
from memoloupe.media.frames import FRAME_EXTRACTION_VERSION, extract_frames
from memoloupe.media.probe import probe_media
from memoloupe.media.quality import QUALITY_DETECTION_VERSION, detect_quality
from memoloupe.media.shots import SHOT_DETECTION_VERSION, detect_shots
from memoloupe.render.shot_html import SHOT_RENDER_VERSION, render_shot_html
from memoloupe.validate.cross_artifact import validate_output_dir
from memoloupe.validate.html_contract import validate_html

#: M1 stub 产物实现版本：内容或文案变化时递增以触发重写。
STUB_VERSION = "stub.v1"

#: 非 artifact 步骤的复用状态文件名（output-dir 根，实现元数据）。
PIPELINE_STATE_FILENAME = ".memoloupe-pipeline.json"

#: 必需步骤：失败立即终止整个阶段。
REQUIRED_STEPS = frozenset(
    {"probe_media", "detect_shots", "extract_frames", "build_clips", "validate"}
)

#: 步骤执行顺序（docs/03 §2.1 DAG 的串行化）。
STEP_ORDER = (
    "acquire_lock",
    "probe_media",
    "detect_shots",
    "extract_frames",
    "build_clips",
    "detect_audio_energy",
    "detect_quality",
    "stub_unavailable",
    "render_shot_html",
    "validate",
)

#: 镜头级 stub 产物及其配置分组。
_STUB_ARTIFACTS = (
    ArtifactName.ASR,
    ArtifactName.MUSIC_FLAGS,
    ArtifactName.AUDIO_CUTS,
    ArtifactName.CAMERA_MOTION,
    ArtifactName.UNIFIED_MEDIA,
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
    """unified-media stub 的 schemaFingerprint（M1 无真实 prompt/schema 版本）。"""
    return fingerprint(
        {"prompt": "none-m1", "schema": "unified-media", "vocab": 1, "parser": 1}
    )


def build_unified_media_stub(clips: list[dict], media: dict, config: dict) -> dict:
    """unified-media.json 降级产物：模型未运行，全部镜头 pending。"""
    model_cfg = config.get("unifiedModel", {})
    shot_statuses = {clip["shotID"]: "pending" for clip in clips}
    return {
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
                "minimumDurationMs": 800,
                "recoveryMinimumDurationMs": 2000,
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
        ) -> dict | None:
            """通用 artifact 步骤：复用判定 → 执行 → 原子写入。失败按必需性处理。"""
            nonlocal hard_failure, any_failed, any_produced
            step_start = time.monotonic()
            if cacheable(name) and store.is_reusable(artifact, fp):
                record(name, "reused", _elapsed(step_start), detail=f"fingerprint={fp}")
                return store.read(artifact)
            try:
                data = fn()
                store.write(artifact, data, WriteMetadata(fingerprint=fp))
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
            record(name, "complete", _elapsed(step_start))
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

            # 3. detect_shots -------------------------------------------------
            shots_fp = fingerprint(
                {
                    "artifact": "shots",
                    "media": media_fp,
                    "config": config_fingerprint(config, ["shots"]),
                    "version": SHOT_DETECTION_VERSION,
                }
            )
            shots_doc = run_artifact_step(
                "detect_shots",
                ArtifactName.SHOTS,
                shots_fp,
                lambda: detect_shots(source, media, config, pool=pool),
            )
            if hard_failure or shots_doc is None:
                return self._report(steps, warnings, out_dir, started, "failed")
            shots: list[dict] = shots_doc["shots"]
            has_audio = bool(media["source"].get("audioTracks"))

            # 4. extract_frames -----------------------------------------------
            frames_fp = fingerprint(
                {
                    "artifact": "frame-evidence",
                    "shots": shots_fp,
                    "version": FRAME_EXTRACTION_VERSION,
                }
            )
            frames = run_artifact_step(
                "extract_frames",
                ArtifactName.FRAME_EVIDENCE,
                frames_fp,
                lambda: extract_frames(source, shots, media, config, out_dir, pool=pool),
            )
            if hard_failure:
                return self._report(steps, warnings, out_dir, started, "failed")

            # 5. build_clips ---------------------------------------------------
            clips_fp = fingerprint(
                {
                    "artifact": "clips",
                    "shots": shots_fp,
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

            # 6. detect_audio_energy -------------------------------------------
            energy_fp = fingerprint(
                {
                    "artifact": "audio-energy",
                    "shots": shots_fp,
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
            )

            # 7. detect_quality -------------------------------------------------
            quality_fp = fingerprint(
                {
                    "artifact": "quality-flags",
                    "shots": shots_fp,
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
            )

            # 8. stub_unavailable ----------------------------------------------
            stub_status = self._stub_step(
                request=request,
                store=store,
                shots=shots,
                media=media,
                clips=clips,
                config=config,
                shots_fp=shots_fp,
                clips_fp=clips_fp,
                cacheable=cacheable("stub_unavailable"),
                record=record,
                warnings=warnings,
            )
            if stub_status == "failed":
                any_failed = True
            elif stub_status in ("complete", "unavailable"):
                any_produced = True

            artifact_fps = {
                "media": media_fp,
                "shots": shots_fp,
                "frame-evidence": frames_fp,
                "clips": clips_fp,
                "audio-energy": energy_fp,
                "quality-flags": quality_fp,
                "stubs": self._stub_fingerprints(shots_fp, clips_fp, config),
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

    def _stub_fingerprints(
        self, shots_fp: str, clips_fp: str, config: dict
    ) -> dict[str, str]:
        return {
            ArtifactName.ASR.value: fingerprint(
                {
                    "artifact": "asr",
                    "version": STUB_VERSION,
                    "config": config_fingerprint(config, ["asr"]),
                }
            ),
            ArtifactName.MUSIC_FLAGS.value: fingerprint(
                {
                    "artifact": "music-flags",
                    "shots": shots_fp,
                    "version": STUB_VERSION,
                    "config": config_fingerprint(config, ["music"]),
                }
            ),
            ArtifactName.AUDIO_CUTS.value: fingerprint(
                {
                    "artifact": "audio-cuts",
                    "shots": shots_fp,
                    "version": STUB_VERSION,
                    "config": config_fingerprint(config, ["audioCuts"]),
                }
            ),
            ArtifactName.CAMERA_MOTION.value: fingerprint(
                {
                    "artifact": "camera-motion",
                    "shots": shots_fp,
                    "version": STUB_VERSION,
                    "config": config_fingerprint(config, ["vision"]),
                }
            ),
            ArtifactName.UNIFIED_MEDIA.value: fingerprint(
                {
                    "artifact": "unified-media",
                    "clips": clips_fp,
                    "version": STUB_VERSION,
                    "config": config_fingerprint(config, ["unifiedModel"]),
                }
            ),
        }

    def _stub_step(
        self,
        *,
        request: ShotAnalysisRequest,
        store: ArtifactStore,
        shots: list[dict],
        media: dict,
        clips: list[dict],
        config: dict,
        shots_fp: str,
        clips_fp: str,
        cacheable: bool,
        record: Callable[..., None],
        warnings: list[str],
    ) -> str:
        """写 5 个 M1 显式降级产物（幂等；逐产物独立复用判定）。"""
        builders: dict[ArtifactName, Callable[[], dict]] = {
            ArtifactName.ASR: build_asr_stub,
            ArtifactName.MUSIC_FLAGS: lambda: build_music_flags_stub(shots, config),
            ArtifactName.AUDIO_CUTS: lambda: build_audio_cuts_stub(shots, config),
            ArtifactName.CAMERA_MOTION: lambda: build_camera_motion_stub(
                shots, media, config
            ),
            ArtifactName.UNIFIED_MEDIA: lambda: build_unified_media_stub(
                clips, media, config
            ),
        }
        fps = self._stub_fingerprints(shots_fp, clips_fp, config)

        step_start = time.monotonic()
        written: list[str] = []
        reused: list[str] = []
        try:
            for artifact in _STUB_ARTIFACTS:
                fp = fps[artifact.value]
                if cacheable and store.is_reusable(artifact, fp):
                    reused.append(artifact.value)
                    continue
                store.write(artifact, builders[artifact](), WriteMetadata(fingerprint=fp))
                written.append(artifact.value)
        except Exception as exc:
            record("stub_unavailable", "failed", _elapsed(step_start), detail=str(exc))
            warnings.append(f"步骤 stub_unavailable 失败：{exc}")
            return "failed"

        if not written:
            record("stub_unavailable", "reused", _elapsed(step_start))
            return "reused"
        warnings.append(
            "M1 显式降级产物：ASR 未配置、BGM/音频切点检测未运行、"
            "Apple Vision 与 UnifiedMLLM 未接入（status=skipped/unavailable）"
        )
        record(
            "stub_unavailable",
            "unavailable",
            _elapsed(step_start),
            detail=f"写入 {len(written)} 个降级产物，复用 {len(reused)} 个",
        )
        return "unavailable"

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
