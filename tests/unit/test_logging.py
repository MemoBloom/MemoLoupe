"""logging 模块单元测试。"""

from __future__ import annotations

import logging
import io

from memoloupe.core import logging as logging_module
from memoloupe.core.logging import get_logger, log_step


def _capture(name: str) -> tuple[logging.Logger, list[logging.LogRecord]]:
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(name)
    logger.handlers = []
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(_Handler())
    return logger, records


def test_adapter_injects_context_fields() -> None:
    _, records = _capture("test.logging.ctx")
    adapter = get_logger("test.logging.ctx", run_id="R1", phase="shot", step="detect")
    adapter.info("hello")
    assert len(records) == 1
    record = records[0]
    assert record.runID == "R1"  # type: ignore[attr-defined]
    assert record.phase == "shot"  # type: ignore[attr-defined]
    assert record.step == "detect"  # type: ignore[attr-defined]
    # 未设置的占位字段有默认值
    assert record.elapsedMs == "-"  # type: ignore[attr-defined]
    assert record.status == "-"  # type: ignore[attr-defined]


def test_adapter_without_context_uses_placeholders() -> None:
    _, records = _capture("test.logging.plain")
    adapter = get_logger("test.logging.plain")
    adapter.warning("warn")
    assert records[0].runID == "-"  # type: ignore[attr-defined]
    assert records[0].levelno == logging.WARNING


def test_log_step_records_status_and_elapsed() -> None:
    _, records = _capture("test.logging.step")
    adapter = get_logger("test.logging.step", run_id="R2", phase="story")
    log_step(adapter, "segment", "complete", 1234, shotCount=3)
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.INFO
    assert record.step == "segment"  # type: ignore[attr-defined]
    assert record.status == "complete"  # type: ignore[attr-defined]
    assert record.elapsedMs == 1234  # type: ignore[attr-defined]
    assert record.runID == "R2"  # type: ignore[attr-defined]
    assert record.shotCount == 3  # type: ignore[attr-defined]


def test_levels() -> None:
    _, records = _capture("test.logging.levels")
    adapter = get_logger("test.logging.levels")
    adapter.debug("d")
    adapter.info("i")
    adapter.warning("w")
    adapter.error("e")
    assert [r.levelno for r in records] == [
        logging.DEBUG,
        logging.INFO,
        logging.WARNING,
        logging.ERROR,
    ]


def test_root_formatter_accepts_third_party_records(monkeypatch) -> None:
    """httpx/fireredvad 等普通 logger 没有 runID，也必须能被 root 格式化。"""
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    stream = io.StringIO()
    try:
        root.handlers = []
        monkeypatch.setattr(logging_module, "_formatter_installed", False)
        get_logger("test.logging.bootstrap")
        assert len(root.handlers) == 1
        root.handlers[0].stream = stream
        logging.getLogger("third.party").info("plain third-party log")
        rendered = stream.getvalue()
        assert "runID=- phase=- step=-" in rendered
        assert "plain third-party log" in rendered
    finally:
        root.handlers = old_handlers
        root.setLevel(old_level)
