"""显式迁移函数集合（docs/02 §7）。

旧名称或旧字段的迁移必须是显式函数，不在读取器里堆叠隐式分支；
迁移保留原文件备份或写到新路径。未来的迁移函数也放在本模块，
并在主入口按顺序调用。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .store import SHOTS_LEGACY_RELATIVE_PATH

_SHOTS_CANONICAL_RELATIVE_PATH = "raw/shots.json"


def _migrate_shot_candidates(root: Path) -> str | None:
    """把旧名 ``raw/shot-candidates.json`` 复制为 ``raw/shots.json``。

    复制而非移动，保留旧文件作为备份；新名已存在时 no-op。
    返回迁移描述，未执行时返回 None。
    """
    legacy = root / SHOTS_LEGACY_RELATIVE_PATH
    canonical = root / _SHOTS_CANONICAL_RELATIVE_PATH
    if not legacy.exists() or canonical.exists():
        return None
    canonical.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(legacy, canonical)
    return f"{SHOTS_LEGACY_RELATIVE_PATH} -> {_SHOTS_CANONICAL_RELATIVE_PATH} (copied)"


def migrate_legacy_names(root: Path) -> list[str]:
    """执行所有旧文件名迁移，返回已执行的迁移描述列表。"""
    root = Path(root)
    done: list[str] = []
    for migrate in (_migrate_shot_candidates,):
        description = migrate(root)
        if description is not None:
            done.append(description)
    return done
