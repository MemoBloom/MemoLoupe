"""``memoloupe shot`` 子命令（docs/01 §10）。

``run_shot_analysis(argv)`` 同时服务包内 CLI 与根级 ``run_shot_analysis.py``
薄包装。退出码：

- ``0`` 完成（含 partial，降级以 warning 呈现）；
- ``2`` 用户参数或配置错误；
- ``3`` 输入文件不存在；
- ``4`` 必要外部工具（ffmpeg/ffprobe）不可用；
- ``5`` 阶段执行失败。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Sequence

from memoloupe.analysis.shot_pipeline import (
    PipelineReport,
    ShotAnalysisPipeline,
    ShotAnalysisRequest,
)
from memoloupe.core.config import load_config
from memoloupe.core.errors import ConfigError

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INPUT = 3
EXIT_TOOL_UNAVAILABLE = 4
EXIT_STAGE_FAILED = 5


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memoloupe shot",
        description="Phase 1 镜头分析：探测、硬切检测、证据抽取、降级产物、HTML 与校验。",
    )
    parser.add_argument("input", type=Path, help="源视频路径")
    parser.add_argument("--output-dir", type=Path, required=True, help="输出目录")
    parser.add_argument("--start-ms", type=int, default=None, help="分析范围起点（毫秒）")
    parser.add_argument("--end-ms", type=int, default=None, help="分析范围终点（毫秒）")
    parser.add_argument(
        "--force",
        action="append",
        default=[],
        metavar="STEP",
        help="强制重跑某步骤（可重复），如 --force detect_shots",
    )
    parser.add_argument("--no-cache", action="store_true", help="忽略全部缓存复用")
    parser.add_argument(
        "--json-report", action="store_true", help="输出机器可读 JSON 报告"
    )
    return parser


def _tool_available(binary: str) -> bool:
    if os.sep in binary:
        return Path(binary).exists()
    return shutil.which(binary) is not None


def _print_summary(report: PipelineReport) -> None:
    print(f"Phase 1 镜头分析：{report.status}（{report.elapsed_ms} ms）")
    for step in report.steps:
        line = f"  {step.name:<22} {step.status:<12} {step.elapsed_ms:>6} ms"
        if step.detail:
            line += f"  {step.detail}"
        print(line)
    for warning in report.warnings:
        print(f"  [warning] {warning}", file=sys.stderr)
    print("产物：")
    for rel in report.artifacts:
        print(f"  {rel}")


def run_shot_analysis(argv: Sequence[str]) -> int:
    args = _build_parser().parse_args(list(argv))

    if not args.input.is_file():
        print(f"错误：输入文件不存在：{args.input}", file=sys.stderr)
        return EXIT_INPUT
    if (args.start_ms is None) != (args.end_ms is None):
        print("错误：--start-ms 与 --end-ms 必须同时提供", file=sys.stderr)
        return EXIT_USAGE
    analyzed_range = (
        (args.start_ms, args.end_ms) if args.start_ms is not None else None
    )

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"错误：配置加载失败：{exc}", file=sys.stderr)
        return EXIT_USAGE

    ffmpeg_cfg = config["ffmpeg"]
    missing = [
        binary
        for binary in (str(ffmpeg_cfg["ffmpegPath"]), str(ffmpeg_cfg["ffprobePath"]))
        if not _tool_available(binary)
    ]
    if missing:
        print(
            f"错误：必要外部工具不可用：{', '.join(missing)}",
            file=sys.stderr,
        )
        return EXIT_TOOL_UNAVAILABLE

    request = ShotAnalysisRequest(
        source=args.input,
        output_dir=args.output_dir,
        analyzed_range=analyzed_range,
        force_steps=frozenset(args.force),
        no_cache=args.no_cache,
        config=config,
    )
    report = ShotAnalysisPipeline().run(request)

    if args.json_report:
        json.dump(report.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        _print_summary(report)

    return EXIT_STAGE_FAILED if report.status == "failed" else EXIT_OK
