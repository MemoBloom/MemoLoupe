"""契约测试共享辅助：fixture 定位与编程派生破坏（mutate/delete）。"""

from __future__ import annotations

import copy
import json
import re
import shutil
from pathlib import Path
from typing import Any, Callable

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"

# 逻辑名 -> output-dir 内相对路径。
ARTIFACT_FILES: list[tuple[str, str]] = [
    ("media", "raw/media.json"),
    ("shots", "raw/shots.json"),
    ("audio-cuts", "raw/audio-cuts.json"),
    ("frame-evidence", "raw/frame-evidence.json"),
    ("asr", "raw/asr.json"),
    ("music-flags", "raw/music-flags.json"),
    ("unified-media", "raw/unified-media.json"),
    ("camera-motion", "raw/camera-motion.json"),
    ("quality-flags", "raw/quality-flags.json"),
    ("audio-energy", "raw/audio-energy.json"),
    ("story-blocks", "raw/story-blocks.json"),
    ("style-profile", "style-profile.json"),
]

_SEGMENT_RE = re.compile(r"([^\.\[\]]+)((?:\[\d+\])*)")
_INDEX_RE = re.compile(r"\[(\d+)\]")


def load_fixture(fixture_dir: str, rel: str) -> dict:
    return json.loads((FIXTURES_DIR / fixture_dir / rel).read_text(encoding="utf-8"))


def _tokens(path: str) -> list[str | int]:
    tokens: list[str | int] = []
    for segment in path.split("."):
        match = _SEGMENT_RE.fullmatch(segment)
        assert match, f"非法路径段: {segment!r}"
        tokens.append(match.group(1))
        tokens.extend(int(m.group(1)) for m in _INDEX_RE.finditer(match.group(2)))
    return tokens


def mutate(data: dict, json_path: str, new_value: Any) -> dict:
    """深拷贝后按 ``a.b[0]`` 路径设值，返回破坏后的副本。"""
    result = copy.deepcopy(data)
    tokens = _tokens(json_path)
    node: Any = result
    for token in tokens[:-1]:
        node = node[token]
    node[tokens[-1]] = new_value
    return result


def delete(data: dict, json_path: str) -> dict:
    """深拷贝后按 ``a.b[0]`` 路径删除键，返回破坏后的副本。"""
    result = copy.deepcopy(data)
    tokens = _tokens(json_path)
    node: Any = result
    for token in tokens[:-1]:
        node = node[token]
    del node[tokens[-1]]
    return result


def copy_output_dir(fixture_dir: str, dst: Path) -> Path:
    """把整个 output-dir 夹具复制到 tmp 目录，供派生破坏用例使用。"""
    target = dst / fixture_dir
    shutil.copytree(FIXTURES_DIR / fixture_dir, target)
    return target


def rewrite(target_root: Path, rel: str, data: dict) -> None:
    """把破坏后的 JSON 写回复制出的 output-dir（保持原子写语义）。"""
    from memoloupe.core.atomic_io import write_json_atomic

    write_json_atomic(target_root / rel, data)


# 派生破坏操作统一签名：对 output_full 的对应文件 dict 做深拷贝破坏。
Mutation = Callable[[dict], dict]
