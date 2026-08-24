"""产物 schema 注册表。

12 个逻辑 artifact 名与 ``schemas/`` 目录下的 Draft 2020-12 JSON Schema
一一对应（文件名即逻辑名加 ``.json``）。所有产物的读写校验都通过
:func:`validate_artifact` 走同一入口，失败时抛出带逻辑名与 JSON 路径的
:class:`~memoloupe.core.errors.ContractError`。
"""

from __future__ import annotations

import json
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import jsonschema

from memoloupe.core.errors import ContractError


class ArtifactName(StrEnum):
    """稳定数据契约中的 12 个 artifact 逻辑名。"""

    MEDIA = "media"
    SHOTS = "shots"
    AUDIO_CUTS = "audio-cuts"
    FRAME_EVIDENCE = "frame-evidence"
    ASR = "asr"
    MUSIC_FLAGS = "music-flags"
    UNIFIED_MEDIA = "unified-media"
    CAMERA_MOTION = "camera-motion"
    QUALITY_FLAGS = "quality-flags"
    AUDIO_ENERGY = "audio-energy"
    STORY_BLOCKS = "story-blocks"
    STYLE_PROFILE = "style-profile"


# 仓库根/schemas（本文件位于 src/memoloupe/artifacts/schemas.py）
SCHEMA_DIR: Path = Path(__file__).resolve().parents[3] / "schemas"


def schema_path(name: ArtifactName) -> Path:
    """返回逻辑名对应的 schema 文件路径。"""
    return SCHEMA_DIR / f"{name.value}.json"


@lru_cache(maxsize=None)
def load_schema(name: ArtifactName) -> dict:
    """加载并缓存逻辑名对应的 JSON Schema。"""
    path = schema_path(name)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ContractError(
            artifact=name.value,
            json_path="$",
            expected="existing schema file",
            actual=f"schema not found: {path}",
        ) from exc
    schema = json.loads(text)
    if not isinstance(schema, dict):
        raise ContractError(
            artifact=name.value,
            json_path="$",
            expected="JSON object schema",
            actual=type(schema).__name__,
        )
    return schema


def _format_json_path(error: jsonschema.ValidationError) -> str:
    """把 jsonschema 的 absolute_path 格式化为 ``$.a.b[0]`` 风格。"""
    path = "$"
    for part in error.absolute_path:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path


def validate_artifact(name: ArtifactName, data: dict) -> None:
    """按逻辑名对应的 schema 校验 data，失败抛 ContractError。

    只报告最优匹配的第一个错误；调用方修正后可再次调用。
    """
    schema = load_schema(name)
    validator = jsonschema.validators.validator_for(schema)(schema)
    error = jsonschema.exceptions.best_match(validator.iter_errors(data))
    if error is None:
        return
    actual = (
        error.instance
        if isinstance(error.instance, (str, int, float, bool)) or error.instance is None
        else type(error.instance).__name__
    )
    raise ContractError(
        artifact=name.value,
        json_path=_format_json_path(error),
        expected=error.message,
        actual=repr(actual),
    )
