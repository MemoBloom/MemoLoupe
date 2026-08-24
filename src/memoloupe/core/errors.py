"""MemoLoupe 异常体系。

所有模块抛出的领域异常都继承自 :class:`MemoLoupeError`，便于上层统一捕获。
"""

from __future__ import annotations


class MemoLoupeError(Exception):
    """MemoLoupe 所有领域异常的基类。"""


class ContractError(MemoLoupeError):
    """数据契约（schema / 状态不变量）被破坏。

    携带 artifact 逻辑名、JSON 路径、期望值与实际值摘要，
    ``__str__`` 输出完整定位信息，便于校验器直接报告。
    """

    def __init__(
        self,
        artifact: str,
        json_path: str,
        expected: str,
        actual: str,
        message: str | None = None,
    ) -> None:
        self.artifact = artifact
        self.json_path = json_path
        self.expected = expected
        self.actual = actual
        self.message = message
        super().__init__(str(self))

    def __str__(self) -> str:
        parts = [
            f"artifact={self.artifact}",
            f"path={self.json_path}",
            f"expected={self.expected}",
            f"actual={self.actual}",
        ]
        if self.message:
            parts.append(f"message={self.message}")
        return f"ContractError({', '.join(parts)})"


class ConfigError(MemoLoupeError):
    """配置合并、类型校验或来源解析失败。"""


class CapabilityUnavailableError(MemoLoupeError):
    """外部能力（模型、ASR、Apple Vision、ffmpeg 等）不可用。

    阶段编排层应捕获该异常并产生显式 unavailable 状态，而不是终止整个阶段。
    """

    def __init__(self, capability: str, reason: str | None = None) -> None:
        self.capability = capability
        self.reason = reason
        super().__init__(str(self))

    def __str__(self) -> str:
        base = f"capability unavailable: {self.capability}"
        return f"{base} ({self.reason})" if self.reason else base


class EvidenceRefError(MemoLoupeError):
    """证据引用（evidenceRefs）非法或无法解析。

    始终携带原始引用字符串；可选携带已解析到的内部路径，
    用于指出指针在哪一段失败。
    """

    def __init__(
        self,
        ref: str,
        reason: str,
        path: str | None = None,
    ) -> None:
        self.ref = ref
        self.reason = reason
        self.path = path
        super().__init__(str(self))

    def __str__(self) -> str:
        parts = [f"ref={self.ref!r}", f"reason={self.reason}"]
        if self.path is not None:
            parts.append(f"path={self.path}")
        return f"EvidenceRefError({', '.join(parts)})"


class ArtifactError(MemoLoupeError):
    """产物读写、路径解析或跨文件引用校验失败。"""

    def __init__(self, artifact: str, reason: str) -> None:
        self.artifact = artifact
        self.reason = reason
        super().__init__(str(self))

    def __str__(self) -> str:
        return f"ArtifactError(artifact={self.artifact}, reason={self.reason})"
