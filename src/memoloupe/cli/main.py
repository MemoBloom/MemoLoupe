"""memoloupe 主 CLI（docs/01 §10）。

子命令：

- ``memoloupe validate TARGET [--strict] [--json-report]``：校验一个 output-dir。
- ``memoloupe shot``：Phase 1+2 合并流程（镜头分析 → 故事分析；--skip-story 只跑 Phase 1）。
- ``memoloupe story``：Phase 2 故事分析（独立重跑入口，如 corrections 使 story 失效后）。
- ``memoloupe profile``：Phase 3 风格档案。
- ``memoloupe connect``：管理模型服务提供商连接（add/status/test/switch/remove/list）。
- ``memoloupe review --output-dir DIR [--port 8765]``：localhost review server。
- ``memoloupe import-corrections FILE --output-dir DIR``：导入离线 corrections。

退出码（docs/01 §10）：

- ``0`` 完成，允许非致命 warning；
- ``2`` 用户参数或配置错误；
- ``3`` 输入/契约错误；
- ``4`` 必要外部工具不可用；
- ``5`` 阶段执行失败；
- ``6`` 校验失败。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from memoloupe.validate.cross_artifact import validate_output_dir
from memoloupe.validate.html_contract import validate_html

from memoloupe.core.config import load_env_file

from .import_corrections import run_import_corrections
from .connect import run_connect
from .profile_build import run_profile_build
from .review import run_review
from .shot_analysis import run_shot_analysis
from .story_analysis import run_story_analysis

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INPUT = 3
EXIT_TOOL_UNAVAILABLE = 4
EXIT_STAGE_FAILED = 5
EXIT_VALIDATION_FAILED = 6


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memoloupe",
        description="MemoLoupe 拉片分析：shot / story / profile 三阶段与产物校验。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser(
        "validate", help="校验 output-dir 的 JSON 契约与跨文件一致性"
    )
    p_validate.add_argument("target", type=Path, help="output-dir 路径")
    p_validate.add_argument(
        "--strict",
        action="store_true",
        help="严格模式：complete 文件必须覆盖全部镜头等",
    )
    p_validate.add_argument(
        "--json-report",
        action="store_true",
        help="输出机器可读 JSON 报告（默认人类可读摘要）",
    )

    # shot/story/profile 的参数由各自子命令模块自行解析，这里用 REMAINDER 透传。
    p_shot = sub.add_parser("shot", help="镜头 + 故事分析（Phase 1+2 合并流程）")
    p_shot.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    p_review = sub.add_parser("review", help="启动 localhost 人工校对 review server")
    p_review.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    p_import = sub.add_parser(
        "import-corrections", help="导入离线导出的 corrections JSON 并重渲染"
    )
    p_import.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    for name, help_text in (
        ("story", "Phase 2：故事分析"),
        ("profile", "Phase 3：风格档案"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    sub.add_parser("config", help="输出脱敏后的有效配置与未配置服务项")

    sub.add_parser("connect", help="连接模型服务提供商（qwen/mimo）")

    return parser


def _cmd_validate(target: Path, *, strict: bool, json_report: bool) -> int:
    if not target.exists() or not target.is_dir():
        print(f"错误：output-dir 不存在或不是目录：{target}", file=sys.stderr)
        return EXIT_INPUT

    issues = validate_output_dir(target, strict=strict)
    # target 中存在 shot/story HTML 时追加 HTML 语义校验（strict 透传）。
    for html_name in ("shot-analysis.html", "story-analysis.html"):
        html_path = target / html_name
        if html_path.is_file():
            issues.extend(validate_html(html_path, root=target, strict=strict))
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]

    if json_report:
        report = {
            "target": str(target),
            "strict": strict,
            "errorCount": len(errors),
            "warningCount": len(warnings),
            "issues": [asdict(i) for i in issues],
        }
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        mode = "strict" if strict else "loose"
        print(f"校验目标：{target}（{mode} 模式）")
        for issue in issues:
            location = f"{issue.artifact}:{issue.json_path}"
            detail = f"（期望 {issue.expected}，实际 {issue.actual}）"
            print(f"  [{issue.severity}] {location} {issue.message}{detail}")
        print(f"结果：{len(errors)} 个错误，{len(warnings)} 个警告")

    return EXIT_VALIDATION_FAILED if errors else EXIT_OK


def _remote_service_is_configured(config: object) -> bool:
    """远程 OpenAI-compatible 服务需要 endpoint、凭据和模型名。"""
    return isinstance(config, dict) and all(
        config.get(key) for key in ("apiKey", "baseUrl", "model")
    )


def _asr_is_configured(config: object) -> bool:
    """按 ASR provider 判断配置是否足以构造服务。

    本地 FireRedVAD + MLX Whisper 不使用远程 ``baseUrl``/``apiKey``/
    ``model``；其模型名位于 ``asr.whisper.model``。可选依赖和 Metal 能力在
    实际转写时检查，不属于静态配置完整性判断。``auto`` 在本地依赖可用或
    远程三项齐全时均视为已配置。
    """
    if not isinstance(config, dict):
        return False
    provider = config.get("provider")
    if provider == "auto":
        from memoloupe.services.asr import local_asr_available

        return local_asr_available() or _remote_service_is_configured(config)
    if provider == "local-fireredvad-mlx":
        whisper = config.get("whisper")
        return isinstance(whisper, dict) and bool(whisper.get("model"))
    return _remote_service_is_configured(config)


def _cmd_config_print() -> int:
    """``memoloupe config``：输出脱敏后的有效配置并标出未配置的真实服务项。"""
    from memoloupe.core.config import load_config, redacted_snapshot

    config = load_config()
    snapshot = redacted_snapshot(config)
    json.dump(snapshot, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    missing: list[str] = []
    if not _asr_is_configured(config.get("asr", {})):
        missing.append("ASR")
    for name, cfg in (
        ("UnifiedMLLM", config.get("unifiedModel", {})),
        ("TextModel", config.get("textModel", {})),
    ):
        if not _remote_service_is_configured(cfg):
            missing.append(name)
    if missing:
        print(f"未配置的真实服务：{'、'.join(missing)}", file=sys.stderr)
    else:
        print("真实服务配置完整", file=sys.stderr)
    return EXIT_OK


def _extract_env_file(argv: Sequence[str]) -> tuple[list[str], str | None]:
    """从 argv 提取 ``--env-file PATH``（或 ``--env-file=PATH``）并移除。

    只匹配主命令参数区，避免与子命令内部同名参数冲突时误删
    （子命令参数以 ``--output-dir`` 等开头，--env-file 由本函数统一消费）。
    """
    out: list[str] = []
    env_file: str | None = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--env-file" and index + 1 < len(argv):
            env_file = argv[index + 1]
            index += 2
        elif arg.startswith("--env-file="):
            env_file = arg.split("=", 1)[1]
            index += 1
        else:
            out.append(arg)
            index += 1
    return out, env_file


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    # argparse.REMAINDER 无法捕获以选项开头的余数（已知限制），shot、review、
    # import-corrections、story、profile 与 connect 的参数全部以选项开头，因此在主
    # parser 之前分流。
    argv, env_file = _extract_env_file(argv)
    injected_env: dict[str, str] = {}
    if env_file is not None:
        # 05-05：--env-file 加载（不覆盖进程已有环境变量）。注入的环境变量
        # 在 main 返回前恢复，避免污染测试进程与重复调用。
        loaded = load_env_file(Path(env_file))
        for key, value in loaded.items():
            if key not in os.environ:
                os.environ[key] = value
                injected_env[key] = value
    try:
        return _dispatch(argv)
    finally:
        for key in injected_env:
            os.environ.pop(key, None)


def _dispatch(argv: Sequence[str]) -> int:
    if argv[:1] == ["shot"]:
        return run_shot_analysis(argv[1:])
    if argv[:1] == ["connect"]:
        return run_connect(argv[1:])
    if argv[:1] == ["review"]:
        return run_review(argv[1:])
    if argv[:1] == ["import-corrections"]:
        return run_import_corrections(argv[1:])
    if argv[:1] == ["story"]:
        return run_story_analysis(argv[1:])
    if argv[:1] == ["profile"]:
        return run_profile_build(argv[1:])

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "validate":
        return _cmd_validate(
            args.target, strict=args.strict, json_report=args.json_report
        )
    if args.command == "shot":
        return run_shot_analysis(args.args)
    if args.command == "config":
        return _cmd_config_print()
    raise SystemExit(f"未知子命令：{args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
