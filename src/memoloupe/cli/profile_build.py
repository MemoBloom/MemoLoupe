"""``memoloupe profile`` 子命令（docs/01 §10、roadmap 04-03）。

``run_profile_build(argv)`` 同时服务包内 CLI 与根级 ``run_profile_build.py``
薄包装。流程：ProfileBuildPipeline（确定性聚合 + 可选模型蒸馏）→ 根目录
原子写入 ``style-profile.json``。

退出码：

- ``0`` 完成（含 partial，降级以 warning 呈现）；
- ``2`` 用户参数或配置错误；
- ``3`` 输入/契约错误（output-dir 缺失、必需产物缺失）；
- ``5`` 阶段执行失败。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Sequence

from memoloupe.analysis.profile_pipeline import (
    PipelineReport,
    ProfileBuildPipeline,
    ProfileBuildRequest,
)
from memoloupe.artifacts.schemas import ArtifactName
from memoloupe.artifacts.store import ArtifactStore
from memoloupe.core.config import load_config
from memoloupe.core.errors import ConfigError, MemoLoupeError
from memoloupe.services.mock import MockTextModelService

from .text_model_config import build_text_model_service

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INPUT = 3
EXIT_STAGE_FAILED = 5

#: prompt 里 slot 行的模式（build_profile_distill_prompt 固定渲染）。
_SLOT_LINE_RE = re.compile(r"^- (S\d{3}) ", re.MULTILINE)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memoloupe profile",
        description="Phase 3 风格档案：确定性聚合 + 可选文本模型蒸馏，写入 style-profile.json。",
    )
    parser.add_argument("--output-dir", type=Path, required=True, help="输出目录（须已有 Phase 1/2 产物）")
    parser.add_argument(
        "--force",
        action="append",
        default=[],
        metavar="STEP",
        help="强制重跑某步骤（可重复），如 --force profile_aggregate",
    )
    parser.add_argument("--no-cache", action="store_true", help="忽略全部缓存复用")
    parser.add_argument(
        "--mock-text-model",
        action="store_true",
        help="文本模型使用可编程 mock（演示/测试用，不发起网络请求）",
    )
    parser.add_argument(
        "--skip-distill",
        action="store_true",
        help="只生成确定性 style-profile，不调用真实或 mock 文本模型蒸馏",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="阶段 partial/failed 时返回非零退出码",
    )
    parser.add_argument(
        "--json-report", action="store_true", help="输出机器可读 JSON 报告"
    )
    return parser


def _print_summary(report: PipelineReport) -> None:
    print(f"Phase 3 风格档案：{report.status}（{report.elapsed_ms} ms）")
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


def _mock_text_service() -> MockTextModelService:
    """按 prompt 中的 slot 行动态回填合法蒸馏响应的 mock。"""

    def respond(request):
        slot_ids = _SLOT_LINE_RE.findall(request.prompt)
        return json.dumps(
            {
                "slots": [
                    {
                        "slotId": sid,
                        "L1": {
                            "functionalTitle": "演示功能",
                            "narrativeFunction": "setup" if i == 0 else "progression",
                            "intendedReaction": "获得信息/学到东西",
                        },
                        "L2": {
                            "carriage": "演示承载",
                            "pattern": "演示模式",
                            "referenceContent": "演示用参考内容。",
                        },
                    }
                    for i, sid in enumerate(slot_ids)
                ],
                "hook": None,
                "payoff": None,
                "structureRequirements": [],
                "adoptionHints": None,
                "discussionItems": [],
            },
            ensure_ascii=False,
        )

    return MockTextModelService(respond)


def run_profile_build(argv: Sequence[str]) -> int:
    args = _build_parser().parse_args(list(argv))
    if args.skip_distill and args.mock_text_model:
        print("错误：--skip-distill 与 --mock-text-model 不能同时使用", file=sys.stderr)
        return EXIT_USAGE

    out_dir: Path = args.output_dir
    if not out_dir.is_dir():
        print(f"错误：output-dir 不存在或不是目录：{out_dir}", file=sys.stderr)
        return EXIT_INPUT

    # 输入门禁：profile 需要 Phase 1（shots/media）与 Phase 2（story-blocks）。
    store = ArtifactStore(out_dir)
    try:
        store.read(ArtifactName("media"))
        store.read(ArtifactName("shots"))
        store.read(ArtifactName("story-blocks"))
    except MemoLoupeError as exc:
        print(
            f"错误：风格档案输入不可用：{exc}\n"
            "（先运行 memoloupe shot 与 memoloupe story）",
            file=sys.stderr,
        )
        return EXIT_INPUT

    text_service = None
    if args.skip_distill:
        print("  [warning] --skip-distill：跳过文本模型蒸馏", file=sys.stderr)
    elif args.mock_text_model:
        text_service = _mock_text_service()
    else:
        try:
            config = load_config()
        except ConfigError as exc:
            print(f"错误：配置不可用：{exc}", file=sys.stderr)
            return EXIT_USAGE
        text_service, warning = build_text_model_service(config)
        if warning:
            print(f"  [warning] {warning}", file=sys.stderr)
    request = ProfileBuildRequest(
        output_dir=out_dir,
        text_service=text_service,
        force=frozenset(args.force),
        no_cache=args.no_cache,
    )
    report = ProfileBuildPipeline().run(request)

    if args.json_report:
        json.dump(report.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        _print_summary(report)

    if report.status == "failed":
        return EXIT_STAGE_FAILED
    if args.strict and report.status == "partial":
        return EXIT_STAGE_FAILED
    return EXIT_OK
