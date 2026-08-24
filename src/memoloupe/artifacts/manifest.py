"""manifest.json 读写。

manifest 结构见 docs/01 §5.2。它只是实现元数据，不是唯一真相：
业务状态仍以各产物文件内部状态与 schema 校验为准，这里只做记录。

所有写回都走 :func:`~memoloupe.core.atomic_io.write_json_atomic`，
不会产生半文件。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from memoloupe.core.atomic_io import read_json, write_json_atomic

from .schemas import ArtifactName

MANIFEST_FILENAME = "manifest.json"
MANIFEST_VERSION = 1


def _manifest_path(root: Path) -> Path:
    return Path(root) / MANIFEST_FILENAME


def _utc_now_iso() -> str:
    """UTC ISO-8601 时间戳，毫秒精度，Z 后缀。"""
    return (
        datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _empty_manifest() -> dict:
    return {
        "manifestVersion": MANIFEST_VERSION,
        "sourceRevisionID": None,
        "artifacts": {},
    }


def load_manifest(root: Path) -> dict:
    """读取 manifest；文件不存在时返回空骨架。"""
    path = _manifest_path(root)
    if not path.exists():
        return _empty_manifest()
    return read_json(path)


def record_artifact(
    root: Path,
    name: ArtifactName | str,
    *,
    path: str,
    fingerprint: str,
    status: str,
    schema_version: str = "1.0",
    legacy_paths: tuple[str, ...] = (),
) -> None:
    """记录或覆盖一个 artifact 条目，并原子写回 manifest。"""
    manifest = load_manifest(root)
    manifest["artifacts"][str(name)] = {
        "path": path,
        "legacyPaths": list(legacy_paths),
        "schemaVersion": schema_version,
        "fingerprint": fingerprint,
        "status": status,
        "updatedAt": _utc_now_iso(),
    }
    write_json_atomic(_manifest_path(root), manifest)


def set_source_revision(root: Path, revision_id: str | None) -> None:
    """记录源文件 revisionID（媒体内容 SHA-256 前 12 位）。"""
    manifest = load_manifest(root)
    manifest["sourceRevisionID"] = revision_id
    write_json_atomic(_manifest_path(root), manifest)
