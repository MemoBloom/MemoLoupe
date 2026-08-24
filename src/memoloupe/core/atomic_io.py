"""原子 JSON / 文本读写。

写入流程固定为：同目录临时文件（tempfile.mkstemp）→ flush → os.fsync →
os.replace。任何一步失败都清理临时文件，保证目标路径要么完整替换、
要么保持旧内容，绝不产生半文件。

JSON 序列化规则固定为 UTF-8、``ensure_ascii=False``、2 空格缩进、
``sort_keys=True``、末尾换行；所有模块共用该规则以保证产物字节稳定。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .errors import ContractError

_JSON_DUMP_KWARGS: dict[str, object] = {
    "ensure_ascii": False,
    "indent": 2,
    "sort_keys": True,
}


def read_json(path: Path) -> dict:
    """读取 UTF-8 JSON 文件并返回顶层 dict。

    JSON 解析失败或顶层不是对象时抛出 :class:`ContractError`。
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ContractError(
            artifact=path.name,
            json_path="$",
            expected="existing UTF-8 JSON file",
            actual="file not found",
        ) from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError(
            artifact=path.name,
            json_path="$",
            expected="valid JSON",
            actual=f"JSONDecodeError at line {exc.lineno} col {exc.colno}: {exc.msg}",
        ) from exc
    if not isinstance(value, dict):
        raise ContractError(
            artifact=path.name,
            json_path="$",
            expected="JSON object",
            actual=type(value).__name__,
        )
    return value


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """把字节原子写入 path；异常时清理临时文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # 清理临时文件，避免残留；忽略清理本身可能的失败。
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def write_json_atomic(path: Path, value: object) -> None:
    """以固定序列化规则原子写入 JSON。"""
    text = json.dumps(value, **_JSON_DUMP_KWARGS) + "\n"
    _write_bytes_atomic(Path(path), text.encode("utf-8"))


def write_text_atomic(path: Path, text: str) -> None:
    """以同样的原子语义写入 UTF-8 文本。"""
    _write_bytes_atomic(Path(path), text.encode("utf-8"))
