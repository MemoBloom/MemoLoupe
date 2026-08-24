"""ArtifactStore：所有产物路径、状态与指纹的唯一入口。

业务模块不得自行拼接 ``raw/foo.json`` 路径（docs/01 §5.1）。
布局约定见 docs/00 §6：除 ``style-profile.json`` 位于输出根目录外，
其余产物都在 ``raw/<name>.json``。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from memoloupe.core.atomic_io import read_json, write_json_atomic
from memoloupe.core.errors import MemoLoupeError

from .manifest import load_manifest, record_artifact
from .schemas import ArtifactName, validate_artifact

# SHOTS 的历史文件名（docs/00 §6），读取时做兼容映射
SHOTS_LEGACY_RELATIVE_PATH = "raw/shot-candidates.json"


@dataclass(frozen=True)
class WriteMetadata:
    """写入产物时随附的实现元数据，记录进 manifest。"""

    fingerprint: str
    status: str = "complete"


class ArtifactStore:
    """以输出目录为根的产物存取入口。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path(self, name: ArtifactName) -> Path:
        """返回产物的规范路径（不做旧名映射）。"""
        if name is ArtifactName.STYLE_PROFILE:
            return self.root / f"{name.value}.json"
        return self.root / "raw" / f"{name.value}.json"

    def _resolve_read_path(self, name: ArtifactName) -> Path:
        """返回读取用路径；SHOTS 在规范路径缺失时回退到旧名。"""
        canonical = self.path(name)
        if name is ArtifactName.SHOTS and not canonical.exists():
            legacy = self.root / SHOTS_LEGACY_RELATIVE_PATH
            if legacy.exists():
                return legacy
        return canonical

    def read(self, name: ArtifactName) -> dict:
        """读取 JSON 并做 schema 校验后返回。"""
        data = read_json(self._resolve_read_path(name))
        validate_artifact(name, data)
        return data

    def write(self, name: ArtifactName, data: dict, metadata: WriteMetadata) -> None:
        """内存校验 → 原子写入 → 更新 manifest。

        schema 校验在写盘前完成，不合法数据不会产生任何文件。
        """
        validate_artifact(name, data)
        target = self.path(name)
        write_json_atomic(target, data)
        record_artifact(
            self.root,
            name,
            path=target.relative_to(self.root).as_posix(),
            fingerprint=metadata.fingerprint,
            status=metadata.status,
            legacy_paths=(
                (SHOTS_LEGACY_RELATIVE_PATH,)
                if name is ArtifactName.SHOTS
                else ()
            ),
        )

    def exists(self, name: ArtifactName) -> bool:
        """产物文件是否存在（含 SHOTS 旧名回退）。"""
        return self._resolve_read_path(name).exists()

    def status(self, name: ArtifactName) -> str | None:
        """从 manifest 读取产物状态；无记录时返回 None。"""
        entry = load_manifest(self.root)["artifacts"].get(name.value)
        if entry is None:
            return None
        return entry.get("status")

    def is_reusable(self, name: ArtifactName, fingerprint: str) -> bool:
        """断点续跑复用判定。

        全部满足才返回 True：文件存在、schema 校验通过、manifest 记录
        fingerprint 匹配、manifest status 为 complete。任何一步不满足
        都返回 False，不抛错。
        """
        path = self._resolve_read_path(name)
        if not path.exists():
            return False
        try:
            validate_artifact(name, read_json(path))
        except MemoLoupeError:
            # 文件损坏或 schema 不合：不可复用，但不抛出
            return False
        entry = load_manifest(self.root)["artifacts"].get(name.value)
        if entry is None:
            return False
        return (
            entry.get("fingerprint") == fingerprint
            and entry.get("status") == "complete"
        )
