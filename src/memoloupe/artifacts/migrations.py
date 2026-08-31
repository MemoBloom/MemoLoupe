"""显式迁移函数集合（docs/02 §7）。

旧名称或旧字段的迁移必须是显式函数，不在读取器里堆叠隐式分支；
迁移保留原文件备份或写到新路径。未来的迁移函数也放在本模块，
并在主入口按顺序调用。
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

from memoloupe.core.atomic_io import read_json, write_json_atomic
from memoloupe.core.errors import ContractError
from memoloupe.core.hashing import fingerprint

from .store import SHOTS_LEGACY_RELATIVE_PATH

_SHOTS_CANONICAL_RELATIVE_PATH = "raw/shots.json"
_UNIFIED_MEDIA_RELATIVE_PATH = "raw/unified-media.json"
_UNIFIED_MEDIA_V1_BACKUP_RELATIVE_PATH = "raw/unified-media.v1.json"


def _take(mapping: dict, key: str, legacy: dict, legacy_path: str) -> object | None:
    """从旧 section 取走字段；存在时同时写入可审计 legacy 扩展。"""
    if key not in mapping:
        return None
    value = mapping.pop(key)
    legacy[legacy_path] = value
    return value


def _upgrade_model_shot_v1_to_v2(shot: dict) -> dict:
    upgraded = copy.deepcopy(shot)
    legacy: dict[str, object] = {}
    _take(upgraded, "evidenceRefs", legacy, "modelShot.evidenceRefs")

    visual = upgraded.get("visual")
    if isinstance(visual, dict):
        _take(visual, "content", legacy, "visual.content")
        _take(visual, "subjectCoverage", legacy, "visual.subjectCoverage")
        _take(visual, "movementIntensity", legacy, "visual.movementIntensity")
        for old, new in (
            ("perspective", "viewpoint"),
            ("lensFeel", "perceivedLensFeel"),
            ("lightingType", "lightingSource"),
            ("colorTemperature", "perceivedColorTemperature"),
            ("texture", "imageTexture"),
        ):
            if old in visual:
                visual[new] = visual.pop(old)

    audio = upgraded.get("audio")
    if isinstance(audio, dict):
        _take(audio, "speech", legacy, "audio.speech")
        if "soundEffects" in audio:
            audio["soundEvents"] = audio.pop("soundEffects")

    components = upgraded.get("components")
    if isinstance(components, dict) and "compositingEvents" in components:
        components["nonTextOverlayEvents"] = components.pop("compositingEvents")

    editing = upgraded.pop("editing", None)
    if isinstance(editing, dict):
        for key in ("transition", "continuity"):
            if key in editing:
                legacy[f"editing.{key}"] = editing[key]

    confidence = upgraded.get("confidence")
    if isinstance(confidence, dict):
        # v1 将 function 与 editing 放在同一请求组，只有 confidence.editing；
        # 迁移时把该组置信度改名为 function，并移除无字段归属的 overall。
        if "function" not in confidence and "editing" in confidence:
            confidence["function"] = confidence["editing"]
        _take(confidence, "editing", legacy, "confidence.editing")
        _take(confidence, "overall", legacy, "confidence.overall")

    if legacy:
        extensions = upgraded.setdefault("extensions", {})
        if not isinstance(extensions, dict):
            extensions = {"preMigrationValue": extensions}
            upgraded["extensions"] = extensions
        extensions["legacyV1"] = legacy
    return upgraded


def upgrade_unified_media_v1_to_v2(document: dict) -> dict:
    """纯函数：把 unified-media v1 显式升级为 v2，不修改输入对象。

    被删除的旧字段保存在每个 modelShot 的 ``extensions.legacyV1``，避免迁移
    造成证据丢失；同时重算 schemaFingerprint，确保缓存不会跨版本复用。
    """
    version = document.get("schemaVersion", 1)
    if version == 2:
        return copy.deepcopy(document)
    if version != 1:
        raise ContractError(
            artifact="unified-media",
            json_path="$.schemaVersion",
            expected="1 or 2",
            actual=version,
        )
    upgraded = copy.deepcopy(document)
    batches = upgraded.get("batches")
    if isinstance(batches, list):
        for batch in batches:
            if not isinstance(batch, dict):
                continue
            response = batch.get("response")
            shots = response.get("shots") if isinstance(response, dict) else None
            if isinstance(shots, list):
                response["shots"] = [
                    _upgrade_model_shot_v1_to_v2(shot)
                    if isinstance(shot, dict) else shot
                    for shot in shots
                ]
    upgraded["schemaVersion"] = 2
    upgraded["schemaFingerprint"] = fingerprint(
        {
            "migration": "unified-media.v1-to-v2",
            "previous": document.get("schemaFingerprint"),
        }
    )
    return upgraded


def migrate_unified_media_v1(root: Path) -> str | None:
    """原子升级 ``raw/unified-media.json``，并保留 ``.v1.json`` 备份。"""
    root = Path(root)
    current = root / _UNIFIED_MEDIA_RELATIVE_PATH
    if not current.exists():
        return None
    document = read_json(current)
    if document.get("schemaVersion", 1) == 2:
        return None
    backup = root / _UNIFIED_MEDIA_V1_BACKUP_RELATIVE_PATH
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        shutil.copy2(current, backup)
    upgraded = upgrade_unified_media_v1_to_v2(document)
    write_json_atomic(current, upgraded)
    return (
        f"{_UNIFIED_MEDIA_RELATIVE_PATH} v1 -> v2 "
        f"(backup: {_UNIFIED_MEDIA_V1_BACKUP_RELATIVE_PATH})"
    )


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
