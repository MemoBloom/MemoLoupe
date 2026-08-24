"""ffmpeg 并发控制（docs/01 §4.4）。

全局信号量限制 ffmpeg 进程并发。信号量在真正启动子进程前获取，
并在异常路径可靠释放（``with`` 语义保证）。
"""

from __future__ import annotations

import threading
from typing import Sequence

from memoloupe.media.proc import ProcessResult, run_process


class FFmpegPool:
    """ffmpeg/ffprobe 全局信号量封装。"""

    def __init__(self, size: int) -> None:
        if size < 1:
            raise ValueError("FFmpegPool size 必须 >= 1")
        self._semaphore = threading.BoundedSemaphore(size)
        self._active = 0
        self._peak = 0
        self._lock = threading.Lock()

    @property
    def peak_concurrency(self) -> int:
        """观测到的峰值并发（性能基线采集用，docs/05 §9）。"""
        with self._lock:
            return self._peak

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_sec: float,
        stdin: bytes | None = None,
        capture_limit_bytes: int | None = None,
    ) -> ProcessResult:
        with self._semaphore:
            with self._lock:
                self._active += 1
                self._peak = max(self._peak, self._active)
            try:
                return run_process(
                    argv,
                    timeout_sec=timeout_sec,
                    stdin=stdin,
                    capture_limit_bytes=capture_limit_bytes,
                )
            finally:
                with self._lock:
                    self._active -= 1
