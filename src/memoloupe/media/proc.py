"""外部进程统一封装（docs/01 §4.3）。

所有 ffprobe/ffmpeg 等外部进程必须经 :func:`run_process` 执行：

- argv 数组，不通过 shell 字符串执行；
- 超时后终止整个进程组；
- stderr 截断，防止日志和内存失控；
- 错误对象保留脱敏命令、退出码和 stderr 尾部。
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Sequence

from memoloupe.core.errors import MemoLoupeError

# stderr 保留的最大字节数（超出后保留尾部）。
STDERR_TAIL_BYTES = 4096


@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    elapsed_sec: float


class ProcessError(MemoLoupeError):
    """外部进程非零退出。携带脱敏命令、退出码与 stderr 尾部。"""

    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        super().__init__(str(self))

    def __str__(self) -> str:
        tail = self.result.stderr.decode("utf-8", errors="replace").strip()
        if len(tail) > 500:
            tail = "…" + tail[-500:]
        return (
            f"ProcessError(rc={self.result.returncode}, "
            f"argv={_redact_argv(self.result.argv)}, stderr_tail={tail!r})"
        )


class ProcessTimeoutError(ProcessError):
    """外部进程超时，进程组已被终止。"""

    def __init__(self, argv: Sequence[str], timeout_sec: float) -> None:
        self.argv_timeout = tuple(argv)
        self.timeout_sec = timeout_sec
        MemoLoupeError.__init__(self, str(self))

    def __str__(self) -> str:
        return (
            f"ProcessTimeoutError(timeout={self.timeout_sec}s, "
            f"argv={_redact_argv(self.argv_timeout)})"
        )


def _redact_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """脱敏命令：任何含 key/token/secret/password 的参数值替换为 ***。"""
    redacted: list[str] = []
    sensitive_next = False
    for arg in argv:
        lower = arg.lower()
        if sensitive_next:
            redacted.append("***")
            sensitive_next = False
            continue
        if any(word in lower for word in ("key", "token", "secret", "password")):
            redacted.append(arg)
            sensitive_next = True
            continue
        redacted.append(arg)
    return tuple(redacted)


def _truncate(data: bytes, limit: int) -> bytes:
    return data if len(data) <= limit else data[-limit:]


def run_process(
    argv: Sequence[str],
    *,
    timeout_sec: float,
    stdin: bytes | None = None,
    capture_limit_bytes: int | None = None,
) -> ProcessResult:
    """执行外部进程并返回结果；非零退出抛 :class:`ProcessError`。

    - 以独立进程组启动（``start_new_session``），超时后 ``killpg`` 终止整组；
    - ``capture_limit_bytes`` 限制 stdout 采集量（超出截断尾部），stderr 始终截断；
    - 不在日志/异常中输出二进制内容。
    """
    args = tuple(str(a) for a in argv)
    if not args:
        raise ValueError("argv 不能为空")

    started = time.monotonic()
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(input=stdin, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        stdout, stderr = proc.communicate()

    elapsed = time.monotonic() - started
    if capture_limit_bytes is not None:
        stdout = _truncate(stdout, capture_limit_bytes)
    stderr = _truncate(stderr, STDERR_TAIL_BYTES)

    if timed_out:
        raise ProcessTimeoutError(args, timeout_sec)

    result = ProcessResult(
        argv=args,
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
        elapsed_sec=elapsed,
    )
    if proc.returncode != 0:
        raise ProcessError(result)
    return result
