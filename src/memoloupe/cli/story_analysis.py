"""``memoloupe story`` 子命令（docs/01 §10、roadmap 03-04）。

``run_story_analysis(argv)`` 同时服务包内 CLI 与根级 ``run_story_analysis.py``
薄包装。流程：门禁（默认要求 shot analysis confirmed）→ StoryAnalysisPipeline
（scaffold + 可选文本模型填充）→ 渲染 story-analysis.html。

草稿门禁（roadmap 03-04）：

- 默认要求 ``raw/shots.json`` 与 ``raw/media.json`` 存在且通过 schema 校验，
  且 ``corrections/shotAnalysis.json`` 显式确认状态为 ``confirmed``，否则
  退出码 3（输入/契约错误）；
- ``--allow-draft`` 显式允许未确认/草稿输入进入阶段管线（跳过门禁，交给
  pipeline 自身校验并显式失败，尽量产出降级产物）。

退出码：

- ``0`` 完成（含 partial，降级以 warning 呈现）；
- ``2`` 用户参数或配置错误；
- ``3`` 输入/契约错误（output-dir 缺失、shot analysis 不可用）；
- ``5`` 阶段执行失败（含渲染失败）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Sequence

from memoloupe.analysis.story_pipeline import (
    PipelineReport,
    StoryAnalysisPipeline,
    StoryAnalysisRequest,
)
from memoloupe.artifacts.schemas import ArtifactName
from memoloupe.artifacts.store import ArtifactStore
from memoloupe.core.config import load_config
from memoloupe.core.errors import ConfigError, MemoLoupeError
from memoloupe.connect.runtime import (
    SOURCE_NONE,
    SOURCE_PROVIDER,
    resolve_active_provider,
)
from memoloupe.connect.store import ConnectionStoreError
from memoloupe.render.corrections import document_status, load_corrections
from memoloupe.render.shot_html import render_shot_html
from memoloupe.render.story_html import render_story_html
from memoloupe.services.mock import MockTextModelService

from .text_model_config import build_text_model_service

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INPUT = 3
EXIT_STAGE_FAILED = 5

#: mock 响应里的叙事字段模板（受控词表合法值；"unknown" 表示无法判断）。
_MOCK_BLOCK_NARRATIVE = {
    "blockTitle": "演示块",
    "divisionAxis": "行动/任务",
    "divisionRationale": "同一行动段落（mock 演示）。",
    "primaryRole": "development",
    "coreContent": "演示用核心内容。",
    "informationRole": "推进新信息",
    "narrativeDensity": "中",
    "audienceReaction": "获得信息/学到东西",
    "visualIndependence": "静音也能看懂",
    "blockRelation": "",
    "relationReason": "",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memoloupe story",
        description="Phase 2 故事分析：确定性聚块 scaffold + 可选文本模型叙事填充 + HTML 渲染。",
    )
    parser.add_argument("--output-dir", type=Path, required=True, help="输出目录（须已有 Phase 1 产物）")
    parser.add_argument("--gap-ms", type=int, default=2000, help="ASR 停顿聚块阈值（毫秒，默认 2000）")
    parser.add_argument(
        "--allow-draft",
        action="store_true",
        help="显式允许未确认/草稿输入（跳过 shot analysis 可用性门禁）",
    )
    parser.add_argument(
        "--force",
        action="append",
        default=[],
        metavar="STEP",
        help="强制重跑某步骤（可重复），如 --force scaffold_story_blocks",
    )
    parser.add_argument("--no-cache", action="store_true", help="忽略全部缓存复用")
    parser.add_argument(
        "--max-blocks",
        type=int,
        default=None,
        metavar="N",
        help="调试模式：只保留前 N 个 block（产物不满足全量覆盖契约，validate 预期报错）",
    )
    parser.add_argument(
        "--mock-text-model",
        action="store_true",
        help="文本模型使用可编程 mock（演示/测试用，不发起网络请求）",
    )
    parser.add_argument(
        "--scaffold-only",
        action="store_true",
        help="只生成确定性 scaffold，不调用真实或 mock 文本模型",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="阶段 partial/failed 或渲染失败时返回非零退出码",
    )
    parser.add_argument(
        "--json-report", action="store_true", help="输出机器可读 JSON 报告"
    )
    return parser


def _print_summary(report: PipelineReport) -> None:
    print(f"Phase 2 故事分析：{report.status}（{report.elapsed_ms} ms）")
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
    """构造按 prompt 中出现的块 ID 动态回填的 mock 文本模型。

    从 prompt 里提取 ``## Bxxxx`` 块标题（prompt 由 story_prompts 固定渲染），
    返回全部块 + 一个聚合 slot 的合法 JSON——保证与 scaffold 的块集合闭合。
    """

    def respond(request):
        block_ids = re.findall(r"^##\s+(B\d{4})\b", request.prompt, flags=re.MULTILINE)
        blocks = []
        for bid in block_ids:
            block = {"storyBlockID": bid, **_MOCK_BLOCK_NARRATIVE}
            blocks.append(block)
        return json.dumps(
            {
                "blocks": blocks,
                "slots": [
                    {
                        "slotID": "S001",
                        "slotType": "行动展开",
                        "slotTitle": "演示槽",
                        "blockIDs": block_ids,
                        "slotRationale": "全部块聚合为一个演示 slot。",
                    }
                ],
            },
            ensure_ascii=False,
        )

    return MockTextModelService(respond)


def _shot_analysis_document_status(out_dir: Path, revision: str) -> str:
    corrections = load_corrections(out_dir, "shotAnalysis")
    return document_status(corrections, revision)


def run_story_analysis(argv: Sequence[str]) -> int:
    args = _build_parser().parse_args(list(argv))
    if args.scaffold_only and args.mock_text_model:
        print("错误：--scaffold-only 与 --mock-text-model 不能同时使用", file=sys.stderr)
        return EXIT_USAGE

    out_dir: Path = args.output_dir
    if not out_dir.is_dir():
        print(f"错误：output-dir 不存在或不是目录：{out_dir}", file=sys.stderr)
        return EXIT_INPUT

    # 草稿门禁：默认要求 shot analysis confirmed；--allow-draft 是显式开发绕过。
    if not args.allow_draft:
        store = ArtifactStore(out_dir)
        try:
            media = store.read(ArtifactName("media"))
            store.read(ArtifactName("shots"))
        except MemoLoupeError as exc:
            print(
                f"错误：shot analysis 不可用：{exc}\n"
                "（先运行 memoloupe shot 并确认 shot-analysis，"
                "或使用 --allow-draft 显式允许未确认输入）",
                file=sys.stderr,
            )
            return EXIT_INPUT
        revision = media.get("source", {}).get("revisionID")
        revision = revision if isinstance(revision, str) else ""
        try:
            status = _shot_analysis_document_status(out_dir, revision)
        except MemoLoupeError as exc:
            print(f"错误：shotAnalysis corrections 不可用：{exc}", file=sys.stderr)
            return EXIT_INPUT
        if status != "confirmed":
            print(
                f"错误：shot analysis 尚未 confirmed（当前状态：{status}）。\n"
                "默认 Phase 2 只接受已确认的镜头分析；"
                "开发/调试可加 --allow-draft 显式允许草稿输入。",
                file=sys.stderr,
            )
            return EXIT_INPUT

    text_service = None
    if args.scaffold_only:
        print("  [warning] --scaffold-only：跳过文本模型填充", file=sys.stderr)
    elif args.mock_text_model:
        text_service = _mock_text_service()
    else:
        try:
            config = load_config()
        except ConfigError as exc:
            print(f"错误：配置不可用：{exc}", file=sys.stderr)
            return EXIT_USAGE
        # connect-first：active provider 叠加到 textModel（含统一服务配置）。
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
        elif service_source == SOURCE_NONE:
            print(
                "  [warning] 未配置模型服务：文本模型将显式降级；"
                "运行 memoloupe connect add qwen 连接 provider",
                file=sys.stderr,
            )
        text_service, warning = build_text_model_service(config)
        if warning:
            print(f"  [warning] {warning}", file=sys.stderr)
    request = StoryAnalysisRequest(
        output_dir=out_dir,
        gap_ms=args.gap_ms,
        allow_draft=args.allow_draft,
        text_service=text_service,
        force=frozenset(args.force),
        no_cache=args.no_cache,
        max_blocks=args.max_blocks,
    )
    report = StoryAnalysisPipeline().run(request)

    # 渲染 story HTML（raw/story-blocks.json 就绪后）。渲染失败以 warning
    # 呈现（HTML 是校对视图，不应击垮阶段结果），但会反映在退出码上。
    render_failed = False
    if report.status != "failed":
        try:
            render_story_html(out_dir)
        except Exception as exc:
            render_failed = True
            print(f"  [warning] 渲染 story-analysis.html 失败：{exc}", file=sys.stderr)
        # D-051：story 结果合并进 shot 工作台。story 完成后必须重渲
        # shot-analysis.html，否则工作台的故事轨道停留在 story 之前的旧状态。
        # 工作台重渲失败只记 warning，不影响 story 产物与退出码。
        try:
            render_shot_html(out_dir)
        except Exception as exc:
            print(
                f"  [warning] 重渲 shot-analysis.html（合并故事轨道）失败：{exc}",
                file=sys.stderr,
            )

    if args.json_report:
        payload = report.to_dict()
        payload["storyHtml"] = "story-analysis.html" if not render_failed else None
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        _print_summary(report)
        if not render_failed:
            print("  story-analysis.html")

    if report.status == "failed" or render_failed:
        return EXIT_STAGE_FAILED
    if args.strict and report.status == "partial":
        return EXIT_STAGE_FAILED
    return EXIT_OK
