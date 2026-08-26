"""``memoloupe shot`` 子命令（docs/01 §10）。

``run_shot_analysis(argv)`` 同时服务包内 CLI 与根级 ``run_shot_analysis.py``
薄包装。退出码：

- ``0`` 完成（含 partial，降级以 warning 呈现；``--strict`` 时 partial 也
  返回 5）；
- ``2`` 用户参数或配置错误；
- ``3`` 输入文件不存在；
- ``4`` 必要外部工具（ffmpeg/ffprobe）不可用；
- ``5`` 阶段执行失败（或 ``--strict`` 下任何 partial）。
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
    SKIPPABLE_STEPS,
    PipelineReport,
    ShotAnalysisPipeline,
    ShotAnalysisRequest,
)
from memoloupe.core.config import load_config
from memoloupe.core.errors import ConfigError, MemoLoupeError
from memoloupe.render.shot_html import render_shot_html

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INPUT = 3
EXIT_TOOL_UNAVAILABLE = 4
EXIT_STAGE_FAILED = 5

#: 05-04 dry-run：等价于显式跳过全部可选步骤（不调用外部服务）。
_DRY_RUN_SKIPS = frozenset(SKIPPABLE_STEPS)


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
        "--mock-services",
        action="store_true",
        help="ASR 与 UnifiedMLLM 使用可编程 mock 服务（演示/测试用，不发起网络请求）",
    )
    parser.add_argument(
        "--align-shot-boundaries-to-audio",
        action="store_true",
        help="把高置信音画同步切的 final 镜头边界对齐到音频切点（detected 边界不变）",
    )
    parser.add_argument(
        "--json-report", action="store_true", help="输出机器可读 JSON 报告"
    )
    # 05-04：生产调试能力。
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="STEP",
        help="显式跳过可选步骤并写降级产物（可重复）：run_asr / detect_music / "
        "detect_audio_cuts / extract_frames / detect_audio_energy / "
        "detect_quality / unified_media_analysis / analyze_camera_motion",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="不调用外部服务：跳过全部可选步骤，只产出切镜/clip/基础产物",
    )
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="只读取已有 raw 产物重渲 shot-analysis.html，不触发检测与模型请求",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="任一步 partial/failed 时返回退出码 5（供 CI 失败门禁）",
    )
    parser.add_argument(
        "--max-shots",
        type=int,
        default=None,
        metavar="N",
        help="调试模式：只分析前 N 个镜头（产物不满足完整范围契约，validate 预期报错）",
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

    if args.render_only:
        return _run_render_only(args.output_dir)

    if not args.input.is_file():
        print(f"错误：输入文件不存在：{args.input}", file=sys.stderr)
        return EXIT_INPUT
    if (args.start_ms is None) != (args.end_ms is None):
        print("错误：--start-ms 与 --end-ms 必须同时提供", file=sys.stderr)
        return EXIT_USAGE
    analyzed_range = (
        (args.start_ms, args.end_ms) if args.start_ms is not None else None
    )
    if args.max_shots is not None and args.max_shots <= 0:
        print("错误：--max-shots 必须为正整数", file=sys.stderr)
        return EXIT_USAGE

    skip_steps = frozenset(args.skip) | (_DRY_RUN_SKIPS if args.dry_run else frozenset())
    unknown = skip_steps - SKIPPABLE_STEPS
    if unknown:
        print(
            f"错误：--skip 含不可跳过的步骤：{sorted(unknown)}\n"
            f"（可跳过：{sorted(SKIPPABLE_STEPS)}）",
            file=sys.stderr,
        )
        return EXIT_USAGE

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
        align_boundaries=args.align_shot_boundaries_to_audio,
        mock_services=args.mock_services,
        skip_steps=skip_steps,
        max_shots=args.max_shots,
    )
    report = ShotAnalysisPipeline().run(request)

    if args.json_report:
        json.dump(report.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        _print_summary(report)

    if report.status == "failed":
        return EXIT_STAGE_FAILED
    if args.strict and report.status == "partial":
        print("--strict：阶段为 partial，按失败处理", file=sys.stderr)
        return EXIT_STAGE_FAILED
    return EXIT_OK


def _run_render_only(out_dir: Path) -> int:
    """05-04 render-only：只重渲 HTML，不触发任何检测与模型请求。"""
    if not out_dir.is_dir():
        print(f"错误：output-dir 不存在或不是目录：{out_dir}", file=sys.stderr)
        return EXIT_INPUT
    try:
        render_shot_html(out_dir)
    except MemoLoupeError as exc:
        print(f"错误：渲染失败：{exc}", file=sys.stderr)
        return EXIT_INPUT
    print(f"render-only：已重渲 {out_dir / 'shot-analysis.html'}")
    return EXIT_OK
