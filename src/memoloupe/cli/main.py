"""memoloupe 主 CLI（docs/01 §10）。

子命令：

- ``memoloupe validate TARGET [--strict] [--json-report]``：校验一个 output-dir。
- ``memoloupe shot/story/profile``：阶段流水线，M0 尚未实现，显式报错。

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
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from memoloupe.validate.cross_artifact import validate_output_dir
from memoloupe.validate.html_contract import validate_html

from .shot_analysis import run_shot_analysis

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
    p_shot = sub.add_parser("shot", help="Phase 1：镜头分析")
    p_shot.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    for name, help_text in (
        ("story", "Phase 2：故事分析"),
        ("profile", "Phase 3：风格档案"),
    ):
        p = sub.add_parser(name, help=f"{help_text}（尚未实现）")
        p.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    return parser


def _cmd_validate(target: Path, *, strict: bool, json_report: bool) -> int:
    if not target.exists() or not target.is_dir():
        print(f"错误：output-dir 不存在或不是目录：{target}", file=sys.stderr)
        return EXIT_INPUT

    issues = validate_output_dir(target, strict=strict)
    # target 中存在 shot-analysis.html 时追加 HTML 语义校验（strict 透传）。
    shot_html = target / "shot-analysis.html"
    if shot_html.is_file():
        issues.extend(validate_html(shot_html, root=target, strict=strict))
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


def _cmd_not_implemented(command: str) -> int:
    print(
        f"错误：子命令 '{command}' 尚未实现（当前里程碑 M0 仅提供 validate）。",
        file=sys.stderr,
    )
    return EXIT_STAGE_FAILED


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "validate":
        return _cmd_validate(
            args.target, strict=args.strict, json_report=args.json_report
        )
    if args.command == "shot":
        return run_shot_analysis(args.args)
    return _cmd_not_implemented(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
