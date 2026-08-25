"""``memoloupe review`` 子命令：启动 localhost 人工校对 review server（docs/04 §5.1）。

退出码：

- ``0`` server 正常退出（KeyboardInterrupt/EOF）；
- ``2`` 用户参数或配置错误；
- ``3`` 输入/契约错误（output-dir 不存在、raw 缺失导致渲染失败）；
- ``5`` server 启动失败（如端口被占用）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from memoloupe.core.errors import ArtifactError, ContractError
from memoloupe.render.review_server import run_review_server
from memoloupe.render.shot_html import render_shot_html

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INPUT = 3
EXIT_STAGE_FAILED = 5


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memoloupe review",
        description="启动 localhost review server，在浏览器中人工校对 shot-analysis。",
    )
    parser.add_argument("--output-dir", type=Path, required=True, help="输出目录")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    return parser


def run_review(argv: Sequence[str]) -> int:
    args = _build_parser().parse_args(list(argv))

    out_dir: Path = args.output_dir
    if not out_dir.is_dir():
        print(f"错误：output-dir 不存在或不是目录：{out_dir}", file=sys.stderr)
        return EXIT_INPUT
    if not (out_dir / "shot-analysis.html").is_file():
        # 启动前先渲染一次，保证首屏可用；raw 缺失导致渲染失败则报错退出。
        try:
            render_shot_html(out_dir, server_mode=True)
        except (ArtifactError, ContractError) as exc:
            print(f"错误：无法渲染 shot-analysis.html：{exc}", file=sys.stderr)
            return EXIT_INPUT
    try:
        run_review_server(out_dir, args.port)
    except OSError as exc:
        print(f"错误：review server 启动失败：{exc}", file=sys.stderr)
        return EXIT_STAGE_FAILED
    return EXIT_OK
