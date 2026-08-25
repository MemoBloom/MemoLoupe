"""Apple Vision 运镜适配器（docs/03 §2.11、docs/01 §7.4）。

定位并（必要时）编译 helpers/apple-vision/main.swift，按
:mod:`memoloupe.vision.protocol` 的协议经子进程执行，把帧级结果聚合为
符合 schemas/camera-motion.json 的产物。

降级策略（helper 崩溃不抛致命异常，docs/03 §7）：非 macOS、swiftc 缺失、
helper 源码缺失、编译失败、运行失败或响应非法，一律返回
:func:`memoloupe.vision.unavailable.build_unavailable_camera_motion`
构造的显式 unavailable 产物，不中断 Phase 1。

二进制缓存：编译输出到 ``<tempdir>/memoloupe-bin/apple-vision-<hash>``，
hash 取自 helper 源码与 swiftc 版本首行，源码或工具链变化自动重编译；
编译产物经临时文件 + os.replace 原子就位。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import tempfile
from pathlib import Path

from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
from memoloupe.core.hashing import fingerprint
from memoloupe.media.proc import ProcessError, run_process
from memoloupe.vision.protocol import (
    DEFAULT_MAX_FRAMES_PER_SHOT,
    DEFAULT_MAX_IMAGE_DIMENSION,
    DEFAULT_SAMPLE_FPS,
    METHOD,
    UNITS,
    VisionProtocolError,
    aggregate_shot_motion,
    build_request,
    parse_response,
)
from memoloupe.vision.unavailable import build_unavailable_camera_motion

CAMERA_MOTION_VERSION = "camera-motion.v1"

#: helper 源码（本文件位于 src/memoloupe/vision/apple_vision.py）
HELPER_SOURCE: Path = Path(__file__).resolve().parents[3] / "helpers" / "apple-vision" / "main.swift"

#: helper 编译/运行默认超时（秒，CALIBRATION；可用 vision.helperTimeoutSec 覆盖）
HELPER_TIMEOUT_SEC = 300.0
#: swiftc 编译超时（秒，CALIBRATION；可用 vision.compileTimeoutSec 覆盖）
COMPILE_TIMEOUT_SEC = 300.0

_BIN_DIR_NAME = "memoloupe-bin"
_BIN_PREFIX = "apple-vision-"


def _helper_cache_dir() -> Path:
    return Path(tempfile.gettempdir()) / _BIN_DIR_NAME


def _swiftc_version(swiftc: str) -> str:
    try:
        result = run_process([swiftc, "--version"], timeout_sec=30.0)
    except (ProcessError, OSError):
        return "unknown"
    first = result.stdout.decode("utf-8", errors="replace").splitlines()
    return first[0] if first else "unknown"


def _helper_binary(swiftc: str, helper_source: Path) -> Path:
    """返回 helper 二进制路径；不存在或源码/工具链过期则编译（原子就位）。

    编译失败抛 ProcessError / OSError，由调用方降级处理。
    """
    cache_dir = _helper_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = fingerprint(
        {
            "source": helper_source.read_bytes().hex(),
            "swiftc": _swiftc_version(swiftc),
            "version": CAMERA_MOTION_VERSION,
        }
    )
    binary = cache_dir / f"{_BIN_PREFIX}{digest}"
    if binary.is_file():
        return binary
    tmp = cache_dir / f".{_BIN_PREFIX}{digest}.{os.getpid()}.tmp"
    try:
        run_process(
            [swiftc, "-O", str(helper_source), "-o", str(tmp)],
            timeout_sec=COMPILE_TIMEOUT_SEC,
        )
        tmp.replace(binary)
    finally:
        tmp.unlink(missing_ok=True)
    return binary


def _shot_time_fields(shot: dict) -> dict:
    start = int(shot["finalStartMs"])
    end = int(shot["finalEndMs"])
    return {
        "shotID": shot["shotID"],
        "sequenceIndex": int(shot["sequenceIndex"]),
        "startMs": start,
        "endMs": end,
        "durationMs": int(shot.get("durationMs", end - start)),
    }


def analyze_camera_motion(
    source: Path,
    shots: list[dict],
    media: dict,
    config: dict,
    *,
    pool=None,
) -> dict:
    """运行 Apple Vision 运镜分析，返回符合 schemas/camera-motion.json 的 dict。

    任何 helper 侧失败（平台/工具链/编译/运行/协议）都降级为
    ``capabilityStatus=unavailable`` 产物，不抛致命异常。
    """
    vision_cfg = config.get("vision", {}) if isinstance(config, dict) else {}
    media_source = media.get("source", {})

    def unavailable(reason: str) -> dict:
        result = build_unavailable_camera_motion(shots, media, config, reason)
        validate_artifact(ArtifactName.CAMERA_MOTION, result)
        return result

    if platform.system() != "Darwin":
        return unavailable("非 macOS 平台，Apple Vision 不可用")
    swiftc = shutil.which("swiftc")
    if swiftc is None:
        return unavailable("swiftc 不可用，无法编译 Apple Vision helper")
    if not HELPER_SOURCE.is_file():
        return unavailable(f"Apple Vision helper 源码缺失: {HELPER_SOURCE}")
    try:
        binary = _helper_binary(swiftc, HELPER_SOURCE)
    except (ProcessError, OSError) as exc:
        return unavailable(f"Apple Vision helper 编译失败: {exc}")

    request = build_request(source, shots, config)
    runner = pool.run if pool is not None else run_process
    timeout = float(vision_cfg.get("helperTimeoutSec", HELPER_TIMEOUT_SEC))
    try:
        result = runner(
            [str(binary)],
            timeout_sec=timeout,
            stdin=json.dumps(request).encode("utf-8"),
        )
    except (ProcessError, OSError) as exc:
        return unavailable(f"Apple Vision helper 运行失败: {exc}")
    try:
        response = parse_response(result.stdout.decode("utf-8", errors="replace"))
    except VisionProtocolError as exc:
        return unavailable(f"Apple Vision helper 响应非法: {exc}")

    frames_by_shot = {shot["shotID"]: shot["frames"] for shot in response["shots"]}
    entries = []
    for shot in shots:
        frames = frames_by_shot.get(shot["shotID"], [])
        entries.append({**_shot_time_fields(shot), **aggregate_shot_motion(frames, config)})

    resolution = media_source.get("resolution", {})
    analysis = {
        "method": METHOD,
        "capabilityStatus": "complete",
        "durationMs": int(media_source.get("durationMs", 0)),
        "sampleFps": float(vision_cfg.get("sampleFps", DEFAULT_SAMPLE_FPS)),
        "maximumFramesPerShot": int(
            vision_cfg.get("maximumFramesPerShot", DEFAULT_MAX_FRAMES_PER_SHOT)
        ),
        "maximumImageDimension": int(
            vision_cfg.get("maximumImageDimension", DEFAULT_MAX_IMAGE_DIMENSION)
        ),
        "opticalFlowEnabled": False,
        "units": UNITS,
        "note": (
            "本版本仅运行 homography registration，optical flow 未启用；"
            "测得的是图像位移，不能单独证明摄影机运动（D-005）"
        ),
    }
    # schema 要求 sourceWidth/Height >= 1（非必填）；media 缺失时不写字段
    if isinstance(resolution, dict):
        for field, key in (("sourceWidth", "width"), ("sourceHeight", "height")):
            value = resolution.get(key)
            if isinstance(value, int) and value >= 1:
                analysis[field] = value
    output = {"analysis": analysis, "shots": entries}
    validate_artifact(ArtifactName.CAMERA_MOTION, output)
    return output
