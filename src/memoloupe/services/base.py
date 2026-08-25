"""外部服务端口公共层（docs/01 §7、docs/00 §7.3）。

- 仅用标准库 urllib 做 HTTP，不引入第三方依赖。
- 错误二分：:class:`TransientServiceError`（429/5xx/网络/超时，可重试）
  与 :class:`PermanentServiceError`（其他 4xx/鉴权失败/响应非 JSON，不重试）。
- 安全不变量：异常与日志文本绝不包含 Authorization 头、API key 或 payload 内容；
  服务端回显了凭据时也会被 :func:`redact_text` 脱敏。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Iterable

from memoloupe.core.errors import MemoLoupeError

SERVICE_PROTOCOL_VERSION = "services.v1"

# 错误详情中保留的服务端响应正文上限，防止日志/内存失控。
_MAX_ERROR_BODY_BYTES = 512

# 值视为机密、需要从错误文本中脱敏的请求头名（小写匹配 + 子串规则）。
_SECRET_HEADER_NAMES = frozenset(
    {"authorization", "proxy-authorization", "x-api-key", "api-key"}
)
_SECRET_HEADER_SUBSTRINGS = ("key", "token", "secret")


# 直连 opener：绕过 macOS 系统代理 / 环境代理，保证请求行为确定
# （否则本机代理会拦截 localhost 或内网服务地址）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ServiceError(MemoLoupeError):
    """外部服务调用失败的基类。"""


class TransientServiceError(ServiceError):
    """可重试的服务错误：HTTP 429/5xx、网络错误、超时。"""


class PermanentServiceError(ServiceError):
    """不可重试的服务错误：其他 4xx、鉴权失败、响应无法解析。"""


def redact_text(text: str, secrets: Iterable[str | None]) -> str:
    """把 ``text`` 中出现的每个非空机密串替换为 ``***``。"""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _header_secrets(headers: dict[str, str]) -> list[str]:
    """从请求头收集需要脱敏的机密值（Authorization、*key*、*token* 等）。"""
    secrets = []
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in _SECRET_HEADER_NAMES or any(
            sub in lowered for sub in _SECRET_HEADER_SUBSTRINGS
        ):
            secrets.append(value)
            # "Bearer sk-xxx" 形式时，裸 key 也单独脱敏。
            if " " in value:
                secrets.append(value.rsplit(" ", 1)[-1])
    return secrets


def http_json_post(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict,
    timeout_sec: float,
) -> dict:
    """POST JSON 并解析 JSON 响应，错误按可重试性分类且全程脱敏。

    - HTTP 429 / 5xx → :class:`TransientServiceError`
    - 其他 4xx（含 400/401/403）→ :class:`PermanentServiceError`
    - 网络错误 / 超时 → :class:`TransientServiceError`
    - 响应非 JSON 或不是对象 → :class:`PermanentServiceError`

    异常信息只含状态码与截断、脱敏后的服务端正文摘要；
    绝不包含请求头、payload 内容。

    说明：为行为确定性，本函数直连目标地址，不走系统/环境代理。
    """
    secrets = _header_secrets(headers)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with _OPENER.open(request, timeout=timeout_sec) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = redact_text(
            exc.read()[:_MAX_ERROR_BODY_BYTES].decode("utf-8", errors="replace"),
            secrets,
        )
        message = f"HTTP {exc.code}: {detail}"
        if exc.code == 429 or exc.code >= 500:
            raise TransientServiceError(message) from None
        raise PermanentServiceError(message) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as exc:
        reason = getattr(exc, "reason", exc)
        raise TransientServiceError(
            f"network error: {redact_text(str(reason), secrets)}"
        ) from None

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PermanentServiceError(
            f"response is not valid JSON: {redact_text(str(exc), secrets)}"
        ) from None
    if not isinstance(data, dict):
        raise PermanentServiceError(
            f"response JSON is not an object: {type(data).__name__}"
        )
    return data
