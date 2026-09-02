"""凭据存储：API key 的存取与脱敏。

机密绝不写入 connections.json，也不进日志。macOS 上默认走系统 Keychain
（subprocess 调 /usr/bin/security，纯标准库，不引 keyring）；
其他平台或无 security 可用时降级为进程内 MemorySecretStore 并告警。
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from typing import Protocol

from memoloupe.services.base import redact_text

logger = logging.getLogger(__name__)

# Keychain 条目固定前缀：service 恒为 memoloupe，account 为 provider:<id>。
_KEYCHAIN_SERVICE = "memoloupe"
_SECURITY_BIN = "/usr/bin/security"


class SecretStore(Protocol):
    """凭据存储接口：按 provider_id 存取机密字符串。"""

    def get(self, provider_id: str) -> str | None: ...
    def set(self, provider_id: str, secret: str) -> None: ...
    def delete(self, provider_id: str) -> None: ...


def _account(provider_id: str) -> str:
    return f"provider:{provider_id}"


class KeychainSecretStore:
    """macOS Keychain 凭据存储（/usr/bin/security CLI）。"""

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        # check=False：非零退出（找不到条目等）由调用方按语义处理；
        # 机密只走 argv（security CLI 惯例），绝不进日志。
        return subprocess.run(
            [_SECURITY_BIN, *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def get(self, provider_id: str) -> str | None:
        result = self._run(
            [
                "find-generic-password",
                "-s",
                _KEYCHAIN_SERVICE,
                "-a",
                _account(provider_id),
                "-w",
            ]
        )
        if result.returncode != 0:
            return None
        return result.stdout.rstrip("\n")

    def set(self, provider_id: str, secret: str) -> None:
        # -U：已存在则更新。
        result = self._run(
            [
                "add-generic-password",
                "-U",
                "-s",
                _KEYCHAIN_SERVICE,
                "-a",
                _account(provider_id),
                "-w",
                secret,
            ]
        )
        if result.returncode != 0:
            from memoloupe.core.errors import MemoLoupeError

            raise MemoLoupeError(
                f"写入 Keychain 失败（provider={provider_id}，exit={result.returncode}）"
            )

    def delete(self, provider_id: str) -> None:
        # 找不到条目不算错误。
        self._run(
            [
                "delete-generic-password",
                "-s",
                _KEYCHAIN_SERVICE,
                "-a",
                _account(provider_id),
            ]
        )


class MemorySecretStore:
    """进程内凭据存储：进程退出即丢失，用于测试与无 Keychain 环境。"""

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}

    def get(self, provider_id: str) -> str | None:
        return self._secrets.get(provider_id)

    def set(self, provider_id: str, secret: str) -> None:
        self._secrets[provider_id] = secret

    def delete(self, provider_id: str) -> None:
        self._secrets.pop(provider_id, None)


def default_secret_store() -> SecretStore:
    """选择默认凭据存储。

    ``MEMOLOUPE_SECRET_STORE=memory`` 强制进程内存储；macOS 且 security
    可用时用 Keychain；否则降级 memory 并记录 warning。
    """
    if os.environ.get("MEMOLOUPE_SECRET_STORE", "").lower() == "memory":
        return MemorySecretStore()
    if platform.system() == "Darwin" and shutil.which(_SECURITY_BIN):
        return KeychainSecretStore()
    logger.warning(
        "Keychain 不可用，凭据将仅保存在进程内存中（进程退出即丢失）；"
        "可设置 MEMOLOUPE_SECRET_STORE=memory 显式确认该行为"
    )
    return MemorySecretStore()


def redact_secret(text: str, secret: str | None) -> str:
    """把 ``text`` 中出现的机密串替换为 ``***``（包装 services.base.redact_text）。"""
    return redact_text(text, [secret])
