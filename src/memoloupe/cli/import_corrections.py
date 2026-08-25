"""``memoloupe import-corrections`` 子命令：导入离线导出的 corrections JSON（docs/04 §5.1 模式 1）。

- FILE 缺失 / JSON 非法 / schema 不过 → 退出码 3 + stderr 原因；
- ``sourceRevisionID`` 与当前 media.json 的 revision 不匹配 → 拒绝导入
  （退出码 3，提示 outdated 语义：旧修正不自动套用新 revision）；
- 合法则把 changes 追加到 ``corrections/shotAnalysis.json``（历史只追加，
  actor 保留文件中的或 ``human``），重渲染 shot-analysis.html，退出 0；
- 导入文件带 ``confirmedAt``/``confirmedBy`` 时一并合并；已 confirmed 时
  重复导入幂等（不重复写确认字段）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from memoloupe.core.errors import ArtifactError, ContractError
from memoloupe.render.corrections import (
    append_changes,
    confirm_corrections,
    corrections_path,
    document_status,
    load_corrections,
)
from memoloupe.render.review_server import (
    current_revision,
    normalize_changes,
    validate_change,
)
from memoloupe.render.shot_html import render_shot_html

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INPUT = 3

DOCUMENT_TYPE = "shotAnalysis"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memoloupe import-corrections",
        description="导入离线导出的 corrections JSON 并重渲染 shot-analysis.html。",
    )
    parser.add_argument("file", type=Path, help="导出的 corrections JSON 文件")
    parser.add_argument("--output-dir", type=Path, required=True, help="输出目录")
    return parser


def _validate_payload(payload: object) -> list[str]:
    """校验导入文件的文档级结构 + 逐条修正必要字段，返回错误列表。"""
    if not isinstance(payload, dict):
        return ["导入文件必须是 corrections JSON 对象"]
    errors: list[str] = []
    if payload.get("correctionVersion") != 1:
        errors.append("correctionVersion 必须为 1")
    document_type = payload.get("documentType")
    if document_type != DOCUMENT_TYPE:
        errors.append(f"documentType 必须为 {DOCUMENT_TYPE!r}，实际 {document_type!r}")
    revision = payload.get("sourceRevisionID")
    if not isinstance(revision, str) or not revision:
        errors.append("sourceRevisionID 必须是非空字符串")
    changes = payload.get("changes")
    if not isinstance(changes, list):
        errors.append("changes 必须是数组")
    else:
        for index, change in enumerate(changes):
            errors.extend(validate_change(change, index))
    return errors


def run_import_corrections(argv: Sequence[str]) -> int:
    args = _build_parser().parse_args(list(argv))

    out_dir: Path = args.output_dir
    if not out_dir.is_dir():
        print(f"错误：output-dir 不存在或不是目录：{out_dir}", file=sys.stderr)
        return EXIT_INPUT
    if not args.file.is_file():
        print(f"错误：导入文件不存在：{args.file}", file=sys.stderr)
        return EXIT_INPUT
    try:
        payload = json.loads(args.file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"错误：导入文件不是合法 JSON：{exc}", file=sys.stderr)
        return EXIT_INPUT

    errors = _validate_payload(payload)
    if errors:
        for error in errors:
            print(f"错误：导入文件 schema 不通过：{error}", file=sys.stderr)
        return EXIT_INPUT

    revision = current_revision(out_dir)
    if payload["sourceRevisionID"] != revision:
        print(
            f"错误：导入文件基于 revision {payload['sourceRevisionID']}，"
            f"当前源 revision 为 {revision or '未知'}；"
            "旧修正不会自动套用到新 revision（outdated），已拒绝导入",
            file=sys.stderr,
        )
        return EXIT_INPUT

    changes = normalize_changes(payload["changes"])
    try:
        append_changes(out_dir, DOCUMENT_TYPE, payload["sourceRevisionID"], changes)
    except ContractError as exc:
        print(f"错误：corrections 落盘校验失败：{exc}", file=sys.stderr)
        return EXIT_INPUT

    # 合并导入文件中的显式确认字段；已 confirmed 时幂等跳过。
    confirmed_at = payload.get("confirmedAt")
    if isinstance(confirmed_at, str) and confirmed_at:
        existing = load_corrections(out_dir, DOCUMENT_TYPE)
        if existing.confirmed_at is None:
            confirmed_by = payload.get("confirmedBy")
            confirm_corrections(
                out_dir,
                DOCUMENT_TYPE,
                actor=confirmed_by
                if isinstance(confirmed_by, str) and confirmed_by
                else "human",
            )

    try:
        render_shot_html(out_dir)
    except (ArtifactError, ContractError) as exc:
        print(f"错误：导入后重渲染失败：{exc}", file=sys.stderr)
        return EXIT_INPUT

    status = document_status(load_corrections(out_dir, DOCUMENT_TYPE), revision)
    print(
        f"已导入 {len(changes)} 条修正到 "
        f"{corrections_path(out_dir, DOCUMENT_TYPE)}；文档状态：{status}"
    )
    return EXIT_OK
