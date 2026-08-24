"""内容哈希与指纹。

- ``content_revision_id``：源文件内容 SHA-256 前 12 位十六进制，
  即契约中的 ``revisionID``（docs/02 §4.1）。流式读取，不整读大文件。
- ``fingerprint``：把任意 dict 规范化（json.dumps，sort_keys、紧凑分隔符、
  ``ensure_ascii=False``）后取 SHA-256 前 16 位，用于配置/请求指纹。

调用方负责不把时间戳、绝对路径或密钥放入 ``fingerprint`` 的 parts。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

_READ_CHUNK_BYTES = 1024 * 1024  # 1 MiB


def content_revision_id(path: Path) -> str:
    """返回文件内容 SHA-256 的前 12 位十六进制（流式读取）。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while True:
            chunk = fh.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()[:12]


def fingerprint(parts: dict) -> str:
    """把 dict 规范化后取 SHA-256 前 16 位十六进制。"""
    canonical = json.dumps(
        parts,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
