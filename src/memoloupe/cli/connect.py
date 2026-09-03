"""``memoloupe connect`` 子命令组：管理模型服务提供商连接。

子命令：add / status / test / switch / remove / list。

- 非机密配置（baseUrl、模型、能力开关）写入 :class:`ConnectionStore`
  （connections.json）；API key 只进 :class:`SecretStore`，绝不落盘到
  connections.json，也绝不打印。
- health check：``GET {baseUrl}/models``（``Authorization: Bearer <key>``，
  超时 15 秒，标准库 urllib，直连不走系统代理）。2xx 通过；网络/非 2xx
  失败只报状态码与脱敏后的错误。

退出码：

- ``0`` 完成（add 的 health check 失败只是 warning，仍返回 0）；
- ``2`` 用户参数或配置错误（未知 provider、缺 API key）；
- ``3`` 输入/状态错误（无已配置 provider、switch/remove 目标不存在、
  add 时凭据写入失败但配置已保存）；
- ``5`` health check 失败（``connect test``）。
"""

from __future__ import annotations

import argparse
import getpass
import os
import socket
import sys
import urllib.error
import urllib.request
from typing import Sequence

from memoloupe.connect.registry import PROVIDERS, ProviderSpec, get_provider_spec
from memoloupe.connect.secrets import SecretStore, default_secret_store
from memoloupe.connect.store import (
    ConnectionStore,
    ConnectionStoreError,
)
from memoloupe.core.errors import MemoLoupeError
from memoloupe.services.base import ServiceError, TransientServiceError, redact_text

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INPUT = 3
EXIT_STAGE_FAILED = 5

#: health check 超时（秒）。
_HEALTH_TIMEOUT_SEC = 15

# 直连 opener：与 services.base 一致，绕过系统/环境代理保证行为确定。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_ONBOARDING_HINT = "尚未配置 provider。先运行：memoloupe connect add qwen"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memoloupe connect",
        description="连接模型服务提供商（qwen/mimo）：添加、查看、测试、切换与删除。",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_add = sub.add_parser("add", help="添加或更新一个 provider 连接")
    p_add.add_argument("provider", help="provider id（如 qwen、mimo）")
    p_add.add_argument(
        "--api-key-env",
        metavar="ENV",
        help="从该环境变量读取 API key（非交互环境必须提供）",
    )
    p_add.add_argument("--base-url", help="覆盖默认 baseUrl")
    p_add.add_argument("--media-model", help="覆盖默认媒体理解模型")
    p_add.add_argument("--text-model", help="覆盖默认文本模型")
    p_add.add_argument("--asr-model", help="覆盖默认 ASR 模型")

    sub.add_parser("status", help="查看已配置连接与当前 provider")

    p_test = sub.add_parser("test", help="对 provider 做 health check（默认当前 provider）")
    p_test.add_argument("provider", nargs="?", help="provider id（缺省为当前 provider）")

    p_switch = sub.add_parser("switch", help="切换当前 provider")
    p_switch.add_argument("provider", help="provider id（须已配置）")

    p_remove = sub.add_parser("remove", help="删除 provider 连接及其已保存凭据")
    p_remove.add_argument("provider", help="provider id（须已配置）")

    sub.add_parser("list", help="列出支持的 provider 及配置状态")
    return parser


def _http_get_status(url: str, *, api_key: str, timeout_sec: float) -> int:
    """GET 请求并返回 HTTP 状态码（含非 2xx）；网络错误转为脱敏后的
    :class:`TransientServiceError`。

    说明：为行为确定性，本函数直连目标地址，不走系统/环境代理。
    """
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with _OPENER.open(request, timeout=timeout_sec) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        # 非 2xx 也是一种明确的服务端回答，按状态码返回由上层判定。
        return exc.code
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as exc:
        reason = getattr(exc, "reason", exc)
        raise TransientServiceError(
            f"network error: {redact_text(str(reason), [api_key])}"
        ) from None


def health_check(
    spec: ProviderSpec, base_url: str, api_key: str
) -> tuple[bool, str]:
    """执行 health check；返回 ``(是否通过, 用户可读说明)``，说明已脱敏。"""
    url = base_url.rstrip("/") + spec.health_check_path
    try:
        status = _http_get_status(url, api_key=api_key, timeout_sec=_HEALTH_TIMEOUT_SEC)
    except ServiceError as exc:
        return False, str(exc)
    if 200 <= status < 300:
        return True, f"HTTP {status}"
    return False, f"HTTP {status}"


def _resolve_api_key(args: argparse.Namespace) -> str | None:
    """解析 API key；失败时打印错误并返回 None。

    优先级：``--api-key-env`` > 交互输入。非 TTY 且无 ``--api-key-env``
    时按缺 key 报错（绝不挂起等输入）。
    """
    if args.api_key_env:
        value = os.environ.get(args.api_key_env, "")
        if not value:
            print(
                f"错误：环境变量 {args.api_key_env} 未设置或为空",
                file=sys.stderr,
            )
            return None
        return value
    if not sys.stdin.isatty():
        print(
            "错误：非交互环境读取 API key 必须提供 --api-key-env ENV",
            file=sys.stderr,
        )
        return None
    key = getpass.getpass("API key（输入不显示）: ").strip()
    if not key:
        print("错误：API key 不能为空", file=sys.stderr)
        return None
    return key


def _prompt_with_default(label: str, default: str | None) -> str | None:
    """交互收集参数：回车取默认值。"""
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def _resolve_param(flag_value: str | None, label: str, default: str | None) -> str | None:
    """flags 优先；缺失时交互环境提问，非交互环境直接取默认。"""
    if flag_value:
        return flag_value
    if sys.stdin.isatty():
        return _prompt_with_default(label, default)
    return default


def _cmd_add(args: argparse.Namespace, store: ConnectionStore, secrets: SecretStore) -> int:
    try:
        spec = get_provider_spec(args.provider)
    except ConnectionStoreError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_USAGE

    api_key = _resolve_api_key(args)
    if api_key is None:
        return EXIT_USAGE

    base_url = _resolve_param(args.base_url, "baseUrl", spec.default_base_url)
    media_model = _resolve_param(args.media_model, "媒体理解模型", spec.default_media_model)
    text_model = _resolve_param(args.text_model, "文本模型", spec.default_text_model)
    asr_model = _resolve_param(args.asr_model, "ASR 模型", spec.default_asr_model)

    record = {
        "providerId": spec.provider_id,
        "baseUrl": base_url,
        "models": {"media": media_model, "text": text_model, "asr": asr_model},
        "capabilities": dict(spec.capabilities),
        # ASR transport（如 mimo-chat）；无 ASR 能力的 provider 为 None。
        "asrTransport": spec.asr_transport,
    }
    try:
        store.upsert_provider(record, make_active=False)
    except ConnectionStoreError as exc:
        print(f"错误：保存连接配置失败：{exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        secrets.set(spec.provider_id, api_key)
    except MemoLoupeError as exc:
        # 凭据存储失败（如 Keychain 拒绝）：连接配置已保存但凭据未保存，
        # 不得抛 traceback；明确说明半完成状态与检查方式。
        print(
            f"错误：连接配置已保存，但凭据写入失败："
            f"{redact_text(str(exc), [api_key])}\n"
            f"当前状态可用 memoloupe connect status 检查；"
            f"修复凭据存储后请重新运行 memoloupe connect add {spec.provider_id}",
            file=sys.stderr,
        )
        return EXIT_INPUT
    print(f"已保存 {spec.provider_id}（{spec.label}）连接配置与凭据")

    ok, detail = health_check(spec, base_url, api_key)
    if ok:
        store.set_active(spec.provider_id)
        print(f"连接测试通过（{detail}），已设为当前 provider")
        print("下一步：")
        print("  memoloupe shot video.mp4 --output-dir out")
        print("  memoloupe connect status")
    else:
        print(
            f"  [warning] 连接测试失败：{detail}"
            "（配置已保存，可稍后运行 memoloupe connect test 重试）",
            file=sys.stderr,
        )
    return EXIT_OK


def _cmd_status(store: ConnectionStore, secrets: SecretStore) -> int:
    data = store.load()
    providers = data["providers"]
    if not providers:
        print(_ONBOARDING_HINT)
        return EXIT_OK
    active = data["activeProvider"]
    for provider_id in sorted(providers):
        record = providers[provider_id]
        marker = "（当前）" if provider_id == active else ""
        models = record["models"]
        print(f"{provider_id}{marker}：")
        print(f"  baseUrl: {record['baseUrl']}")
        print(
            "  models: "
            f"media={models['media']} text={models['text']} asr={models.get('asr')}"
        )
        print(f"  secret: {'已保存' if secrets.get(provider_id) else '未保存'}")
    return EXIT_OK


def _cmd_test(args: argparse.Namespace, store: ConnectionStore, secrets: SecretStore) -> int:
    data = store.load()
    provider_id = args.provider if args.provider else data["activeProvider"]
    if provider_id is None or provider_id not in data["providers"]:
        print(f"错误：{_ONBOARDING_HINT}", file=sys.stderr)
        return EXIT_INPUT
    record = data["providers"][provider_id]
    api_key = secrets.get(provider_id)
    if not api_key:
        print(
            f"错误：provider {provider_id!r} 缺少凭据，"
            f"请重新运行 memoloupe connect add {provider_id}",
            file=sys.stderr,
        )
        return EXIT_INPUT
    spec = get_provider_spec(provider_id)
    ok, detail = health_check(spec, record["baseUrl"], api_key)
    if ok:
        print(f"{provider_id} 连接正常（{detail}）")
        return EXIT_OK
    print(f"错误：{provider_id} 连接失败：{detail}", file=sys.stderr)
    return EXIT_STAGE_FAILED


def _cmd_switch(args: argparse.Namespace, store: ConnectionStore) -> int:
    try:
        store.set_active(args.provider)
    except ConnectionStoreError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_INPUT
    print(f"已切换当前 provider：{args.provider}")
    return EXIT_OK


def _cmd_remove(args: argparse.Namespace, store: ConnectionStore, secrets: SecretStore) -> int:
    try:
        store.remove_provider(args.provider)
    except ConnectionStoreError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_INPUT
    secrets.delete(args.provider)
    print(f"已删除 {args.provider}（含已保存凭据）")
    return EXIT_OK


def _cmd_list(store: ConnectionStore) -> int:
    data = store.load()
    for provider_id in sorted(PROVIDERS):
        spec = PROVIDERS[provider_id]
        if data["activeProvider"] == provider_id:
            state = "当前"
        elif provider_id in data["providers"]:
            state = "已配置"
        else:
            state = "未配置"
        print(f"{provider_id:<8} {spec.label}  [{state}]")
    if not data["providers"]:
        print(_ONBOARDING_HINT)
    return EXIT_OK


def run_connect(
    argv: Sequence[str],
    *,
    store: ConnectionStore | None = None,
    secrets: SecretStore | None = None,
) -> int:
    """``memoloupe connect`` 入口；``store``/``secrets`` 可注入（测试用）。"""
    args = _build_parser().parse_args(list(argv))
    if store is None:
        store = ConnectionStore()
    if secrets is None:
        secrets = default_secret_store()

    if args.subcommand == "add":
        return _cmd_add(args, store, secrets)
    if args.subcommand == "status":
        return _cmd_status(store, secrets)
    if args.subcommand == "test":
        return _cmd_test(args, store, secrets)
    if args.subcommand == "switch":
        return _cmd_switch(args, store)
    if args.subcommand == "remove":
        return _cmd_remove(args, store, secrets)
    if args.subcommand == "list":
        return _cmd_list(store)
    raise SystemExit(f"未知 connect 子命令：{args.subcommand}")
