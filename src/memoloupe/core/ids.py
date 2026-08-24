"""ID 格式校验与生成（docs/02 §1.3）。

格式约定：

- 镜头：``SH`` + 4 位数字（``SH0001``）
- 音频切点：``AU`` + 4 位数字（``AU0001``）
- 帧证据：``F_<shotID>_<MAIN|Knn>``（``F_SH0001_MAIN``、``F_SH0001_K01``）
- 故事块：``B`` + 4 位数字（``B0001``）
- 故事插槽：``S`` + 3 位数字（``S001``）

代码不得仅凭字符串推断实体类型；生成与校验一律经由本模块。
"""

from __future__ import annotations

import re

_SHOT_ID_RE = re.compile(r"^SH\d{4}$")
_AUDIO_BOUNDARY_ID_RE = re.compile(r"^AU\d{4}$")
_FRAME_EVIDENCE_ID_RE = re.compile(r"^F_(SH\d{4})_(MAIN|K\d{2})$")
_STORY_BLOCK_ID_RE = re.compile(r"^B\d{4}$")
_STORY_SLOT_ID_RE = re.compile(r"^S\d{3}$")


def is_valid_shot_id(value: object) -> bool:
    return isinstance(value, str) and bool(_SHOT_ID_RE.fullmatch(value))


def is_valid_audio_boundary_id(value: object) -> bool:
    return isinstance(value, str) and bool(_AUDIO_BOUNDARY_ID_RE.fullmatch(value))


def is_valid_frame_evidence_id(value: object) -> bool:
    return isinstance(value, str) and bool(_FRAME_EVIDENCE_ID_RE.fullmatch(value))


def is_valid_story_block_id(value: object) -> bool:
    return isinstance(value, str) and bool(_STORY_BLOCK_ID_RE.fullmatch(value))


def is_valid_story_slot_id(value: object) -> bool:
    return isinstance(value, str) and bool(_STORY_SLOT_ID_RE.fullmatch(value))


def make_shot_id(index: int) -> str:
    """由 1 起始的序号生成镜头 ID，如 ``make_shot_id(1) == "SH0001"``。"""
    return f"SH{index:04d}"


def make_audio_boundary_id(index: int) -> str:
    return f"AU{index:04d}"


def make_main_frame_evidence_id(shot_id: str) -> str:
    """代表帧证据 ID，如 ``F_SH0001_MAIN``。"""
    return f"F_{validate_shot_id(shot_id)}_MAIN"


def make_keyframe_evidence_id(shot_id: str, keyframe_index: int) -> str:
    """关键帧证据 ID，如 ``F_SH0001_K01``（keyframe_index 从 1 开始）。"""
    if not 1 <= keyframe_index <= 99:
        raise ValueError(f"关键帧序号必须在 1..99: {keyframe_index!r}")
    return f"F_{validate_shot_id(shot_id)}_K{keyframe_index:02d}"


def make_story_block_id(index: int) -> str:
    return f"B{index:04d}"


def make_story_slot_id(index: int) -> str:
    return f"S{index:03d}"


def _validate(value: str, predicate, kind: str) -> str:
    if not predicate(value):
        raise ValueError(f"非法{kind} ID: {value!r}")
    return value


def validate_shot_id(value: str) -> str:
    """校验并返回镜头 ID；非法时抛 ValueError。"""
    return _validate(value, is_valid_shot_id, "镜头")


def validate_audio_boundary_id(value: str) -> str:
    return _validate(value, is_valid_audio_boundary_id, "音频切点")


def validate_frame_evidence_id(value: str) -> str:
    return _validate(value, is_valid_frame_evidence_id, "帧证据")


def validate_story_block_id(value: str) -> str:
    return _validate(value, is_valid_story_block_id, "故事块")


def validate_story_slot_id(value: str) -> str:
    return _validate(value, is_valid_story_slot_id, "故事插槽")
