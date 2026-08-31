"""结构化日志（docs/01 §11）。

只使用标准库 logging，INFO/WARNING/ERROR/DEBUG 分级。每条记录通过
``LoggerAdapter`` 携带 runID/phase/step 等上下文字段，格式包含
elapsedMs/status 占位。禁止输出二进制内容、Data URI、完整模型返回或凭据。
"""

from __future__ import annotations

import logging

_LOG_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s "
    "runID=%(runID)s phase=%(phase)s step=%(step)s "
    "elapsedMs=%(elapsedMs)s status=%(status)s %(message)s"
)

_DEFAULT_CONTEXT = {
    "runID": "-",
    "phase": "-",
    "step": "-",
    "elapsedMs": "-",
    "status": "-",
}

_formatter_installed = False


class _ContextDefaultsFilter(logging.Filter):
    """为不经过 MemoLoupe LoggerAdapter 的第三方记录补齐格式字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in _DEFAULT_CONTEXT.items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


def _ensure_formatter() -> None:
    """确保 root logger 至少有一个带上下文字段格式的 handler。"""
    global _formatter_installed
    if _formatter_installed:
        return
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.addFilter(_ContextDefaultsFilter())
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    _formatter_installed = True


class _ContextAdapter(logging.LoggerAdapter):
    """把 runID/phase/step 等上下文字段注入每条记录。"""

    def process(self, msg, kwargs):
        extra = dict(_DEFAULT_CONTEXT)
        extra.update(self.extra)
        extra.update(kwargs.get("extra") or {})
        kwargs["extra"] = extra
        return msg, kwargs


def get_logger(
    name: str,
    *,
    run_id: str | None = None,
    phase: str | None = None,
    step: str | None = None,
) -> logging.LoggerAdapter:
    """返回携带 runID/phase/step 上下文的 LoggerAdapter。"""
    _ensure_formatter()
    context: dict[str, str] = {}
    if run_id is not None:
        context["runID"] = run_id
    if phase is not None:
        context["phase"] = phase
    if step is not None:
        context["step"] = step
    return _ContextAdapter(logging.getLogger(name), context)


def log_step(
    logger: logging.LoggerAdapter,
    step: str,
    status: str,
    elapsed_ms: int,
    **extra: object,
) -> None:
    """记录一个步骤的完成状态（INFO 级），附带 elapsedMs/status 及额外字段。"""
    fields = {"step": step, "status": status, "elapsedMs": elapsed_ms}
    fields.update(extra)
    logger.info("%s", step, extra=fields)
