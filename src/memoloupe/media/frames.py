"""帧证据抽取（docs/03 §2.5，schemas/frame-evidence.json）。

每镜头抽一张代表帧：final 区间中点，极短镜头向区间内部夹紧，
绝不取精确 finalEndMs。抽帧失败进入 failedFrames，不伪造 fileRef，
其余镜头继续（docs/03 §7 降级矩阵）。
"""

from __future__ import annotations

from pathlib import Path

from memoloupe.artifacts.schemas import ArtifactName, validate_artifact
from memoloupe.core.ids import make_main_frame_evidence_id
from memoloupe.media.proc import ProcessError

FRAME_EXTRACTION_VERSION = "frames.v1"

# 输出帧宽度 / JPEG 质量（docs/03 §2.5 默认宽度 640）
FRAME_WIDTH = 640
JPEG_QUALITY = 5


def representative_time_ms(final_start_ms: int, final_end_ms: int) -> int:
    """代表帧时间：final 区间中点，夹紧到 ``[startMs, endMs - 1]``。

    半开区间的中点天然不会落到精确 finalEndMs 上；极短镜头（<40ms）
    同样由夹紧保证取到区间内部。
    """
    if final_end_ms <= final_start_ms:
        raise ValueError(f"非法镜头区间: [{final_start_ms}, {final_end_ms})")
    midpoint = (final_start_ms + final_end_ms) // 2
    return min(max(midpoint, final_start_ms), final_end_ms - 1)


def frame_file_ref(evidence_id: str) -> str:
    """帧文件相对 out_dir 的路径（正斜杠）。"""
    return f"evidence/frames/{evidence_id}.jpg"


def input_cache_key(revision_id: str | None) -> str:
    """输入视频缓存键：original + revision 前 4 位（当前直接用原始源抽帧）。"""
    if not revision_id:
        return "original-unknown"
    return f"original-{revision_id[:4]}"


def _frame_argv(ffmpeg: str, source: Path, time_ms: int, out_path: Path) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-ss", f"{time_ms / 1000:.3f}",
        "-i", str(source),
        "-frames:v", "1",
        "-vf", f"scale={FRAME_WIDTH}:-2",
        "-q:v", str(JPEG_QUALITY),
        str(out_path),
    ]


def extract_frames(
    source: Path,
    shots: list[dict],
    media: dict,
    config: dict,
    out_dir: Path,
    *,
    pool,
) -> dict:
    """为每个镜头抽取代表帧，返回符合 frame-evidence.json 的 dict。

    - 文件名由 evidenceID 决定（``F_SHxxxx_MAIN.jpg``），写入
      ``out_dir/evidence/frames/``；
    - 单帧失败（ffmpeg 报错或无输出文件）记入 failedFrames，其余镜头继续。
    """
    source = Path(source)
    out_dir = Path(out_dir)
    frames_dir = out_dir / "evidence" / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg = config["ffmpeg"]["ffmpegPath"]
    timeout = float(config["ffmpeg"]["frameTimeoutSec"])
    revision_id = (media.get("source") or {}).get("revisionID")

    frames: list[dict] = []
    failed: list[dict] = []
    for shot in shots:
        shot_id = shot["shotID"]
        start_ms = int(shot["finalStartMs"])
        end_ms = int(shot["finalEndMs"])
        evidence_id = make_main_frame_evidence_id(shot_id)
        time_ms = representative_time_ms(start_ms, end_ms)
        out_path = frames_dir / f"{evidence_id}.jpg"

        reason: str | None = None
        try:
            pool.run(
                _frame_argv(ffmpeg, source, time_ms, out_path),
                timeout_sec=timeout,
            )
            if not out_path.is_file() or out_path.stat().st_size == 0:
                reason = "ffmpeg 未产生输出文件"
        except ProcessError as exc:
            reason = str(exc)

        if reason is not None:
            failed.append(
                {
                    "evidenceID": evidence_id,
                    "shotID": shot_id,
                    "timeMs": time_ms,
                    "reason": reason,
                }
            )
            continue

        frames.append(
            {
                "evidenceID": evidence_id,
                "frameID": evidence_id,
                "shotID": shot_id,
                "frameType": "representative",
                "timeMs": time_ms,
                "range": {"startMs": time_ms, "endMs": time_ms},
                "fileRef": frame_file_ref(evidence_id),
                "quality": "usable",
                "summary": "agent visual review required",
            }
        )

    result = {
        "status": "failed" if shots and not frames else "complete",
        "version": FRAME_EXTRACTION_VERSION,
        "request": {
            "sourceRevisionID": revision_id,
            "inputVideo": str(source.expanduser().resolve()),
            "inputCacheKey": input_cache_key(revision_id),
            "width": FRAME_WIDTH,
            "jpegQuality": JPEG_QUALITY,
        },
        "extraction": {
            "mode": "auto",
            "workerCount": int(config["ffmpeg"]["globalConcurrency"]),
            "cachedFrames": 0,
        },
        "frames": frames,
        "failedFrames": failed,
    }
    validate_artifact(ArtifactName.FRAME_EVIDENCE, result)
    return result
