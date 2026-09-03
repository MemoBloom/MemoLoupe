"""pytest 全局配置。

connect 子系统隔离：任何测试都不得读写真实用户目录下的
``~/.config/memoloupe/connections.json`` 或系统 Keychain——
connect 管道解析（``resolve_active_provider``）在这台机器上存在真实
provider 时会污染无关测试。所有测试默认指向 tmp_path 下的空连接文件 +
进程内凭据存储；需要覆盖的测试在自己的 fixture 里重新 setenv 即可。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_connect_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMOLOUPE_CONNECTIONS_PATH", str(tmp_path / "connections.json"))
    monkeypatch.setenv("MEMOLOUPE_SECRET_STORE", "memory")
