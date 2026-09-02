"""connect 子包：连接存储、凭据存储与 provider 注册表。"""

from __future__ import annotations

from memoloupe.connect.registry import PROVIDERS, ProviderSpec, get_provider_spec
from memoloupe.connect.secrets import (
    KeychainSecretStore,
    MemorySecretStore,
    SecretStore,
    default_secret_store,
    redact_secret,
)
from memoloupe.connect.store import (
    ConnectionStore,
    ConnectionStoreError,
    default_connections_path,
)

__all__ = [
    "PROVIDERS",
    "ConnectionStore",
    "ConnectionStoreError",
    "KeychainSecretStore",
    "MemorySecretStore",
    "ProviderSpec",
    "SecretStore",
    "default_connections_path",
    "default_secret_store",
    "get_provider_spec",
    "redact_secret",
]
