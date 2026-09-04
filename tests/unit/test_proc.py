"""media/proc 与 media/concurrency 单元测试。"""

from __future__ import annotations

import sys
import threading
import time

import pytest

from memoloupe.media.concurrency import FFmpegPool
from memoloupe.media.proc import (
    STDERR_TAIL_BYTES,
    ProcessError,
    ProcessTimeoutError,
    run_process,
)


class TestRunProcess:
    def test_echo_roundtrip(self):
        result = run_process(
            [sys.executable, "-c", "print('hello')"], timeout_sec=10
        )
        assert result.returncode == 0
        assert result.stdout.strip() == b"hello"
        assert result.elapsed_sec >= 0

    def test_nonzero_exit_raises_process_error(self):
        with pytest.raises(ProcessError) as exc_info:
            run_process(
                [sys.executable, "-c", "import sys; sys.exit(3)"], timeout_sec=10
            )
        assert exc_info.value.result.returncode == 3
        assert "rc=3" in str(exc_info.value)

    def test_stderr_tail_preserved_in_error(self):
        with pytest.raises(ProcessError) as exc_info:
            run_process(
                [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(1)"],
                timeout_sec=10,
            )
        assert "boom" in str(exc_info.value)

    def test_stderr_truncated_to_tail(self):
        result_source = (
            "import sys; sys.stderr.write('x' * 100000); sys.exit(1)"
        )
        with pytest.raises(ProcessError) as exc_info:
            run_process([sys.executable, "-c", result_source], timeout_sec=10)
        assert len(exc_info.value.result.stderr) <= STDERR_TAIL_BYTES

    def test_timeout_kills_process_group(self):
        # 子进程忽略 SIGTERM 并长时间睡眠；超时后进程组被 SIGKILL，调用应立即返回。
        code = (
            "import os, signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "open(sys.argv[1], 'w').write('started')\n"
            "time.sleep(60)\n"
        )
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "started")
            start = time.monotonic()
            with pytest.raises(ProcessTimeoutError):
                run_process(
                    [sys.executable, "-c", code, marker],
                    timeout_sec=0.5,
                )
            elapsed = time.monotonic() - start
            # kill 后 communicate 应立即返回，远小于 60s sleep
            assert elapsed < 10

    def test_stdin_passthrough(self):
        result = run_process(
            [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
            timeout_sec=10,
            stdin=b"abc",
        )
        assert result.stdout.strip() == b"ABC"

    def test_capture_limit_truncates_stdout(self):
        result = run_process(
            [sys.executable, "-c", "print('y' * 10000)"],
            timeout_sec=10,
            capture_limit_bytes=100,
        )
        assert len(result.stdout) <= 100

    def test_empty_argv_rejected(self):
        with pytest.raises(ValueError):
            run_process([], timeout_sec=1)

    def test_sensitive_args_redacted_in_error(self):
        with pytest.raises(ProcessError) as exc_info:
            run_process(
                [sys.executable, "--api-key", "supersecret", "-c",
                 "import sys; sys.exit(1)"],
                timeout_sec=10,
            )
        text = str(exc_info.value)
        assert "supersecret" not in text
        assert "***" in text


class TestFFmpegPool:
    def test_rejects_invalid_size(self):
        with pytest.raises(ValueError):
            FFmpegPool(0)

    def test_run_delegates(self):
        pool = FFmpegPool(2)
        result = pool.run([sys.executable, "-c", "print(1)"], timeout_sec=10)
        assert result.returncode == 0

    def test_concurrency_limited_and_peak_tracked(self):
        pool = FFmpegPool(2)
        barrier_release = threading.Event()
        started = []
        lock = threading.Lock()

        def worker():
            pool.run(
                [sys.executable, "-c",
                 "import time; time.sleep(0.2)"],
                timeout_sec=10,
            )
            with lock:
                started.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        barrier_release.set()

        assert len(started) == 4
        assert pool.peak_concurrency <= 2
        assert pool.peak_concurrency >= 1

    def test_semaphore_released_on_error(self):
        pool = FFmpegPool(1)
        with pytest.raises(ProcessError):
            pool.run([sys.executable, "-c", "import sys; sys.exit(1)"], timeout_sec=10)
        # 信号量若未释放，第二次调用会死锁超时
        result = pool.run([sys.executable, "-c", "print('ok')"], timeout_sec=10)
        assert result.stdout.strip() == b"ok"
