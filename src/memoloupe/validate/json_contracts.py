"""单文件 JSON Schema 校验（docs/04 §7.1）。

``validate_file`` 不抛异常，而是收集全部 schema 违规并以
:class:`ValidationIssue` 列表返回，供 CLI 汇总报告。底层用
``jsonschema.Draft202012Validator.iter_errors`` 遍历全部错误，
而不是在第一个错误处停止。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from memoloupe.artifacts.schemas import ArtifactName, load_schema

Severity = Literal["error", "warning", "info"]


@dataclass(frozen=True)
class ValidationIssue:
    """一条校验发现。

    - ``severity``：``error`` 表示契约被破坏；``warning`` 表示非严格模式下
      允许但需要注意；``info`` 仅提示（例如文件缺失导致检查被跳过）。
    - ``artifact``：产物逻辑名（见 :class:`ArtifactName`）。
    - ``json_path``：``$.a.b[0]`` 形式的定位路径。
    - ``expected`` / ``actual``：期望值与实际值摘要，均为短字符串。
    """

    severity: Severity
    artifact: str
    json_path: str
    message: str
    expected: str
    actual: str


def _format_path(error: ValidationError) -> str:
    path = "$"
    for part in error.absolute_path:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path


def _summarize(value: object, limit: int = 80) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _describe_expected(error: ValidationError) -> str:
    return _summarize({error.validator: error.validator_value}, limit=120)


def validate_file(name: ArtifactName, data: dict) -> list[ValidationIssue]:
    """对单个产物执行 schema 校验，返回全部 error 级 issue（可能为空）。"""
    name = ArtifactName(name)
    validator = Draft202012Validator(load_schema(name))
    errors = sorted(
        validator.iter_errors(data),
        key=lambda e: ([str(p) for p in e.absolute_path], e.message),
    )
    return [
        ValidationIssue(
            severity="error",
            artifact=name.value,
            json_path=_format_path(error),
            message=error.message,
            expected=_describe_expected(error),
            actual=_summarize(error.instance),
        )
        for error in errors
    ]
