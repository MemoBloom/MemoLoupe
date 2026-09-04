"""切点边界证据帧（Phase 06-03，schemas/shot-relations.json evidence）。

每个相邻镜头对生成两张证据帧：

- ``left-exit``：左镜头 finalEndMs 前的最后一帧（夹紧到区间内部）；
- ``right-entry``：右镜头 finalStartMs 后的第一帧（同样夹紧）。

绝不取精确 finalEndMs（半开区间约定）；抽帧失败写显式状态，不伪造
fileRef，其余 pair 继续（docs/03 降级矩阵）。
"""

from __future__ import annotations

from bisect import bisect_left
from pathlib import Path

from memoloupe.media.proc import ProcessError

TRANSITION_EVIDENCE_VERSION = "transition-evidence.v1"

JPEG_QUALITY = 5


def pair_id_for_index(index: int) -> str:
    """TR0001 风格的切点序号（1 起）。"""
    return f"TR{index:04d}"


def evidence_file_ref(tr_id: str, side: str) -> str:
    """证据帧相对 out_dir 路径（正斜杠）。``side`` ∈ {left-exit, right-entry}。"""
    return f"evidence/transitions/{tr_id}-{side}.jpg"


def boundary_frame_times_from_pts(
    pts_ms: list[int] | None, *, boundary_ms: int, left_end_ms: int, right_start_ms: int
) -> tuple[int, int]:
    """由真实帧 PTS 索引定位切点两侧边界帧时刻。

    - left-exit：严格小于 boundaryMs 的最后一帧（左镜头最后一展示帧）；
    - right-entry：大于等于 boundaryMs 的第一帧（右镜头首展示帧）。

    二分查找，长片（10^5 帧量级 × N-1 pair）不退化为线性扫描。
    索引不可用（None/空/切点超出范围）时退化为 ``endMs-1`` /
    ``startMs``；此时低帧率下两者可能落在同一展示帧，luma 差将失去
    意义，调用方须自行标记证据降级。
    """
    if pts_ms:
        left_pos = bisect_left(pts_ms, boundary_ms)  # 第一个 >= boundary
        if 0 < left_pos < len(pts_ms):
            return pts_ms[left_pos - 1], pts_ms[left_pos]
    return left_end_ms - 1, right_start_ms


def _frame_argv(
    ffmpeg: str, source: Path, time_ms: int, out_path: Path, width: int
) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-ss", f"{time_ms / 1000:.3f}",
        "-i", str(source),
        "-frames:v", "1",
        "-vf", f"scale={width}:-2",
        "-q:v", str(JPEG_QUALITY),
        str(out_path),
    ]


def extract_boundary_frame(
    source: Path,
    out_dir: Path,
    file_ref: str,
    time_ms: int,
    *,
    ffmpeg_path: str,
    width: int,
    timeout_sec: float,
    pool,
) -> None:
    """抽取单张边界帧到 ``out_dir / file_ref``。

    进程失败抛 :class:`ProcessError`；成功但无输出文件抛 :class:`ValueError`。
    """
    out_path = out_dir / file_ref
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pool.run(
        _frame_argv(ffmpeg_path, source, time_ms, out_path, width),
        timeout_sec=timeout_sec,
    )
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise ValueError("ffmpeg 未产生边界帧输出文件")


def measure_frame_luma(
    image_path: Path,
    *,
    ffmpeg_path: str,
    timeout_sec: float,
    pool,
) -> float:
    """用 ffmpeg signalstats 测量单帧平均亮度，归一化到 [0, 1]。

    进程失败抛 :class:`ProcessError`；无 YAVG 输出抛 :class:`ValueError`。
    """
    argv = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel", "info",
        "-i", str(image_path),
        "-vf",
        "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
        "-f", "null",
        "-",
    ]
    result = pool.run(argv, timeout_sec=timeout_sec)
    text = result.stdout.decode("utf-8", errors="replace")
    for line in text.splitlines():
        token = line.strip()
        if token.startswith("lavfi.signalstats.YAVG="):
            yavg = float(token.split("=", 1)[1])
            return max(0.0, min(yavg / 255.0, 1.0))
    raise ValueError("ffmpeg signalstats 未输出 YAVG")


def extract_transition_evidence(
    source: Path,
    shots: list[dict],
    config: dict,
    out_dir: Path,
    *,
    pool,
    frame_times: dict[str, dict[str, int]] | None = None,
) -> dict:
    """为全部相邻 pair 抽取 A/B 边界帧。

    ``frame_times[pairID][side]`` 提供由 PTS 索引定位的取样时刻；缺省时
    退化为左镜头 endMs-1 / 右镜头 startMs。

    返回 ``{pairID: {"left-exit": {...}, "right-entry": {...}}}``，
    每项含 ``status/fileRef?/timeMs?/reason?``，失败不中断其余 pair。
    """
    source = Path(source)
    out_dir = Path(out_dir)
    ffmpeg = str(config["ffmpeg"]["ffmpegPath"])
    timeout = float(config["ffmpeg"]["frameTimeoutSec"])
    width = int(config.get("reviewTimeline", {}).get("transitionFrameWidth", 320))

    evidence: dict[str, dict] = {}
    for index in range(1, len(shots)):
        left = shots[index - 1]
        right = shots[index]
        pair_id = f"{left['shotID']}--{right['shotID']}"
        tr_id = pair_id_for_index(index)
        times = (frame_times or {}).get(pair_id, {})
        entry: dict[str, dict] = {}
        for side, shot in (
            ("left-exit", left),
            ("right-entry", right),
        ):
            time_ms = times.get(side)
            if time_ms is None:
                start_ms = int(shot["finalStartMs"])
                end_ms = int(shot["finalEndMs"])
                time_ms = max(end_ms - 1, start_ms)
            file_ref = evidence_file_ref(tr_id, side)
            try:
                extract_boundary_frame(
                    source,
                    out_dir,
                    file_ref,
                    int(time_ms),
                    ffmpeg_path=ffmpeg,
                    width=width,
                    timeout_sec=timeout,
                    pool=pool,
                )
            except (ProcessError, ValueError) as exc:
                entry[side] = {
                    "status": "failed",
                    "timeMs": int(time_ms),
                    "reason": str(exc),
                }
                continue
            entry[side] = {
                "status": "complete",
                "fileRef": file_ref,
                "timeMs": int(time_ms),
            }
        evidence[pair_id] = entry
    return evidence
