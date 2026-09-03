"""``memoloupe shot`` 子命令（docs/01 §10）。

shot+story 合并流程（D-056）：默认在 Phase 1 镜头分析完成后链式执行
Phase 2 故事分析（隐式 ``--allow-draft``——合并流程的校对发生在统一工作台
之后，corrections 使 story 失效后用独立的 ``memoloupe story`` 重跑）；
``--skip-story`` 只跑 Phase 1。``--render-only`` 不触发 story。

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
from memoloupe.cli.story_analysis import run_story_analysis
from memoloupe.connect.runtime import (
    SOURCE_NONE,
    SOURCE_PROVIDER,
    resolve_active_provider,
)
from memoloupe.connect.store import ConnectionStoreError
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
        description="镜头 + 故事分析（Phase 1+2 合并流程）：探测、硬切检测、证据抽取、"
        "故事聚块、降级产物、HTML 与校验。",
    )
    parser.add_argument("input", type=Path, help="源视频路径")
    parser.add_argument("--output-dir", type=Path, required=True, help="输出目录")
    parser.add_argument("--start-ms", type=int, default=None, help="分析范围起点（毫秒）")
    parser.add_argument("--end-ms", type=int, default=None, help="分析范围终点（毫秒）")
    parser.add_argument(
        "--skip-story",
        action="store_true",
        help="只跑 Phase 1 镜头分析；默认 shot 完成后自动继续故事分析（合并流程）",
    )
    parser.add_argument(
        "--gap-ms",
        type=int,
        default=2000,
        help="故事聚块的 ASR 停顿阈值（毫秒，默认 2000；合并流程传给 story）",
    )
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
        "detect_quality / detect_motion_effects / unified_media_analysis / "
        "analyze_camera_motion",
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

    # connect-first：active provider 叠加到 unifiedModel/textModel/asr；
    # --mock-services 时模型配置不被消费，跳过解析。
    if not args.mock_services:
        try:
            config, service_source = resolve_active_provider(config)
        except ConnectionStoreError as exc:
            print(f"错误：连接配置不可用：{exc}", file=sys.stderr)
            return EXIT_USAGE
        if service_source == SOURCE_PROVIDER:
            print(
                "  [connect] 已加载当前 provider 的模型配置"
                "（memoloupe connect status 查看）",
                file=sys.stderr,
            )
        elif service_source == SOURCE_NONE and (
            {"run_asr", "unified_media_analysis"} - skip_steps
        ):
            print(
                "  [warning] 未配置模型服务：ASR/UnifiedMLLM 将显式降级；"
                "运行 memoloupe connect add qwen 连接 provider",
                file=sys.stderr,
            )

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
    if args.skip_story:
        return EXIT_OK
    return _run_chained_story(args)


def _run_chained_story(args: argparse.Namespace) -> int:
    """shot+story 合并流程（D-056）：shot 成功后链式执行 story。

    隐式 ``--allow-draft``：独立 ``memoloupe story`` 的 confirmed 门禁面向
    "先校对镜头、再跑故事"的两段式工作流；合并流程的校对发生在统一工作台
    之后，corrections 使 story 失效后用独立 story 命令重跑。

    mock/dry-run 语义透传：``--mock-services`` → story 用 mock 文本模型；
    ``--dry-run`` → story 只出确定性 scaffold（不调用文本模型）。
    """
    story_argv = [
        "--output-dir",
        str(args.output_dir),
        "--allow-draft",
        "--gap-ms",
        str(args.gap_ms),
    ]
    if args.no_cache:
        story_argv.append("--no-cache")
    if args.strict:
        story_argv.append("--strict")
    if args.mock_services:
        story_argv.append("--mock-text-model")
    if args.dry_run:
        story_argv.append("--scaffold-only")
    print("── 故事分析（shot+story 合并流程）──")
    return run_story_analysis(story_argv)


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
