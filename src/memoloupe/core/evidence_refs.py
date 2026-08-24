"""证据引用（evidenceRefs）解析与取值（docs/02 §2）。

引用形如：

- ``raw/media.json#source.durationMs``
- ``raw/shots.json#shots[0]``
- ``raw/unified-media.json#batches[0].response.shots[0].visual.framing``
- ``clips/SH0001.mp4``（文件证据，无 ``#``）
- ``evidence/frames/F_SH0001_MAIN.jpg``（文件证据）

解析器拒绝绝对路径（``/`` 或 ``~`` 开头）、含 ``..`` 段和反斜杠的引用。
JSON 指针采用当前约定的点号分段 + ``[n]`` 数组下标语法；
契约 1.0 必须兼容该语法（未来可迁移到 RFC 6901）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import EvidenceRefError

_SEGMENT_RE = re.compile(r"^([^\[\]]+)((?:\[\d+\])*)$")
_INDEX_RE = re.compile(r"\[(\d+)\]")


@dataclass(frozen=True)
class EvidenceRef:
    """解析后的证据引用。

    - ``file_path``：``#`` 前的相对文件路径（全部引用都有）。
    - ``json_path``：``#`` 后的 JSON 指针；文件证据为 None。
    - ``is_file_evidence``：True 表示直接引用媒体/图片文件本身。
    """

    file_path: str
    json_pointer: str | None
    is_file_evidence: bool


def _check_file_path(ref: str, file_path: str) -> None:
    if not file_path:
        raise EvidenceRefError(ref, "文件路径为空")
    if file_path.startswith(("/", "~")):
        raise EvidenceRefError(ref, "拒绝绝对路径", path=file_path)
    if "\\" in file_path:
        raise EvidenceRefError(ref, "拒绝反斜杠", path=file_path)
    if any(segment == ".." for segment in file_path.split("/")):
        raise EvidenceRefError(ref, "拒绝包含 '..' 的逃逸路径", path=file_path)


def parse_evidence_ref(ref: str) -> EvidenceRef:
    """解析单条证据引用；非法时抛 :class:`EvidenceRefError`。"""
    if not isinstance(ref, str) or not ref:
        raise EvidenceRefError(str(ref), "引用为空或不是字符串")
    if "#" in ref:
        file_path, _, pointer = ref.partition("#")
        if not pointer:
            raise EvidenceRefError(ref, "'#' 后缺少 JSON 指针", path=file_path)
        _check_file_path(ref, file_path)
        return EvidenceRef(
            file_path=file_path, json_pointer=pointer, is_file_evidence=False
        )
    _check_file_path(ref, ref)
    return EvidenceRef(file_path=ref, json_pointer=None, is_file_evidence=True)


def _tokenize_pointer(pointer: str, ref_for_error: str) -> list[str | int]:
    """把 ``shots[0].visual`` 拆成 ``["shots", 0, "visual"]``。"""
    if not pointer:
        raise EvidenceRefError(ref_for_error, "JSON 指针为空")
    tokens: list[str | int] = []
    for segment in pointer.split("."):
        match = _SEGMENT_RE.match(segment)
        if not match:
            raise EvidenceRefError(
                ref_for_error, f"非法指针段: {segment!r}", path=pointer
            )
        key, index_part = match.group(1), match.group(2)
        tokens.append(key)
        tokens.extend(int(m.group(1)) for m in _INDEX_RE.finditer(index_part))
    return tokens


def resolve_json_pointer(doc: dict, pointer: str) -> object:
    """按点号 + ``[n]`` 语法在文档中取值。

    键缺失、类型不符或数组下标越界时抛 :class:`EvidenceRefError`，
    并携带已走过的路径便于定位。
    """
    tokens = _tokenize_pointer(pointer, ref_for_error=pointer)
    current: object = doc
    walked: list[str] = []
    for token in tokens:
        if isinstance(token, int):
            if not isinstance(current, list):
                raise EvidenceRefError(
                    pointer, "对非数组使用下标", path=".".join(walked)
                )
            if token >= len(current) or token < 0:
                raise EvidenceRefError(
                    pointer, f"数组下标越界: [{token}]", path=".".join(walked)
                )
            current = current[token]
            walked.append(f"[{token}]")
        else:
            if not isinstance(current, dict):
                raise EvidenceRefError(
                    pointer, "对非对象使用键", path=".".join(walked)
                )
            if token not in current:
                raise EvidenceRefError(
                    pointer, f"键缺失: {token!r}", path=".".join(walked)
                )
            current = current[token]
            walked.append(token)
    return current
