"""analysis.vocabulary — rules/vocabulary.json 受控词表加载与归一化。

契约见 docs/02_DATA_AND_STATE_CONTRACTS.md §3.3。
归一化不丢弃原始字符串；模型无法映射的值返回 unmapped 并保留原文，
交由 Observation 层落到 ``unmapped`` 状态。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from memoloupe.core.packaged import packaged_path

TRANSITION_SEPARATOR = " → "

_DEFAULT_VOCABULARY_PATH = packaged_path("rules", "vocabulary.json")


@dataclass(frozen=True)
class NormalizationResult:
    """单字段归一化结果。``original`` 始终保留传入的原始内容。"""

    status: Literal["value", "unmapped", "unknown"]
    value: str | None
    original: object


@dataclass(frozen=True)
class FieldRule:
    values: tuple[str, ...]
    aliases: dict[str, str]
    allow_transitions: bool = False
    multi_value_separator: str | None = None

    def lookup(self, token: str) -> str | None:
        """单个 token 的规范值；先精确命中 values，再按别名（大小写不敏感）命中。"""
        token = token.strip()
        if token in self.values:
            return token
        return self.aliases.get(token.casefold())


class Vocabulary:
    """受控词表。字段未登记时不报错，由调用方决定如何落状态。"""

    def __init__(self, version: int, fields: dict[str, FieldRule]) -> None:
        self.version = version
        self.fields = fields

    def normalize(self, field: str, raw: object) -> NormalizationResult:
        if raw is None:
            return NormalizationResult("unknown", None, raw)
        rule = self.fields.get(field)
        if rule is None:
            return NormalizationResult("unknown", None, raw)
        if not isinstance(raw, str):
            return NormalizationResult("unmapped", None, raw)
        text = raw.strip()
        if not text or text.casefold() == "unknown":
            return NormalizationResult("unknown", None, raw)

        if rule.allow_transitions and TRANSITION_SEPARATOR in text:
            return self._normalize_parts(raw, text.split(TRANSITION_SEPARATOR), TRANSITION_SEPARATOR, rule)
        if rule.multi_value_separator and rule.multi_value_separator in text:
            return self._normalize_parts(
                raw, text.split(rule.multi_value_separator), rule.multi_value_separator, rule
            )

        hit = rule.lookup(text)
        if hit is not None:
            return NormalizationResult("value", hit, raw)
        return NormalizationResult("unmapped", None, raw)

    def _normalize_parts(
        self, raw: object, parts: list[str], joiner: str, rule: FieldRule
    ) -> NormalizationResult:
        """逐项归一化；任一项不命中则整体 unmapped，避免箭头/顿号内外自由文本绕过词表。"""
        normalized: list[str] = []
        for part in parts:
            hit = rule.lookup(part) if part.strip() else None
            if hit is None:
                return NormalizationResult("unmapped", None, raw)
            normalized.append(hit)
        return NormalizationResult("value", joiner.join(normalized), raw)

    def canonical_key(self, field: str, value: str) -> str | None:
        """返回该值的规范键；无法归入词表（含 unknown）时返回 None。"""
        result = self.normalize(field, value)
        return result.value if result.status == "value" else None

    def prompt_fragment(self, field: str) -> str:
        """生成注入模型 prompt 的允许值说明文本。"""
        rule = self.fields.get(field)
        if rule is None:
            return f"{field}：自由文本，无受控词表约束。"
        parts = [f"{field} 允许值：{'、'.join(rule.values)}"]
        if rule.allow_transitions:
            parts.append(f'可用 "{TRANSITION_SEPARATOR.strip()}" 连接表示变化，每段必须来自允许值')
        if rule.multi_value_separator:
            parts.append(f'可用 "{rule.multi_value_separator}" 连接多个允许值')
        if rule.aliases:
            parts.append("系统会自动归一化常见英文别名，但应直接输出允许值")
        return "；".join(parts) + "。"


def load_vocabulary(path: Path | None = None) -> Vocabulary:
    """加载受控词表；默认读取仓库根 rules/vocabulary.json。"""
    vocab_path = Path(path) if path is not None else _DEFAULT_VOCABULARY_PATH
    if not vocab_path.is_file():
        raise FileNotFoundError(f"vocabulary file not found: {vocab_path}")
    data = json.loads(vocab_path.read_text(encoding="utf-8"))
    fields: dict[str, FieldRule] = {}
    for name, spec in data["fields"].items():
        aliases = spec.get("aliases", {})
        fields[name] = FieldRule(
            values=tuple(spec["values"]),
            aliases={key.casefold(): value for key, value in aliases.items()},
            allow_transitions=bool(spec.get("allowTransitions", False)),
            multi_value_separator=spec.get("multiValueSeparator"),
        )
    return Vocabulary(version=int(data["version"]), fields=fields)
