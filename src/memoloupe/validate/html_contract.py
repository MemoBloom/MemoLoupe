"""validate.html_contract — shot/story analysis HTML 语义校验器（docs/04 §8）。

只用标准库 :class:`html.parser.HTMLParser`，避免依赖浏览器纠错行为：

- §8.1 结构：document type/status 枚举、必需 metadata、id 唯一、
  table/tbody/tr/td 嵌套（缺 tbody 报具体错误）、story-block 禁区、
  shotAnalysis 至少一个镜头列；
- §8.2 单元格：data-value-state 五态 + labelOnly、五态必备
  confidence/source/verified、verified 取值、evidence refs 可解析、
  每镜头至少一个可追溯证据单元格；
- §8.3 安全：禁外链 script、禁 javascript: URL、禁 http(s) 外链 img/script/link；
- §8.4 严格模式（需提供 output-dir root）：页面 shotID 集合与
  final 边界对齐 raw/shots.json，source revision 对齐 raw/media.json。

所有 issue 携带行号（HTMLParser.getpos()）。
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from memoloupe.core.errors import ContractError, EvidenceRefError
from memoloupe.core.atomic_io import read_json
from memoloupe.core.evidence_refs import parse_evidence_ref
from memoloupe.validate.json_contracts import ValidationIssue

HTML_CONTRACT_VERSION = "htmlcheck.v1"

DOCUMENT_TYPES = frozenset({"shotAnalysis", "storyAnalysis"})
DOCUMENT_STATUSES = frozenset({"draft", "underReview", "confirmed", "outdated"})
VALUE_STATES = frozenset(
    {"value", "absent", "absent-claimed", "unknown", "unmapped", "labelOnly"}
)
FIVE_STATES = VALUE_STATES - {"labelOnly"}

#: 参与嵌套结构跟踪的标签。
_STRUCTURAL_TAGS = frozenset({"table", "thead", "tbody", "tr", "td", "th"})
#: 含 src/href 且禁止指向 http(s) 的标签。
_REMOTE_FORBIDDEN_TAGS = frozenset({"img", "script", "link"})


def _issue(
    artifact: str,
    line: int,
    tag: str,
    message: str,
    expected: str = "",
    actual: str = "",
    *,
    severity: str = "error",
) -> ValidationIssue:
    return ValidationIssue(
        severity=severity,  # type: ignore[arg-type]
        artifact=artifact,
        json_path=f"L{line} <{tag}>",
        message=message,
        expected=expected,
        actual=actual,
    )


class _Checker(HTMLParser):
    """单遍 HTML 结构/单元格/安全检查器。"""

    def __init__(self, artifact: str) -> None:
        super().__init__(convert_charrefs=True)
        self.artifact = artifact
        self.issues: list[ValidationIssue] = []
        self.html_attrs: dict[str, str] | None = None
        self.html_line = 0
        self.ids: dict[str, int] = {}
        self._stack: list[str] = []
        self._table_stack: list[dict[str, object]] = []
        # 镜头列：th/td 同时带 data-shot-id/data-start-ms/data-end-ms。
        self.shot_columns: list[tuple[str, str, str, int]] = []
        # 每个镜头的可追溯证据单元格计数。
        self.evidence_cells: dict[str, int] = {}
        self.story_block_lines: list[int] = []

    # ------------------------------------------------------------------
    # HTMLParser 回调
    # ------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        line, _ = self.getpos()
        attr_map = {name: (value if value is not None else "") for name, value in attrs}

        if tag == "html" and self.html_attrs is None:
            self.html_attrs = attr_map
            self.html_line = line

        element_id = attr_map.get("id")
        if element_id:
            if element_id in self.ids:
                self.issues.append(_issue(
                    self.artifact, line, tag,
                    f"id 重复：{element_id!r}（首次出现于 L{self.ids[element_id]}）",
                    expected="唯一 id", actual=element_id,
                ))
            else:
                self.ids[element_id] = line

        self._check_security(tag, attr_map, line)

        classes = attr_map.get("class", "").split()
        if "story-block" in classes:
            self.story_block_lines.append(line)

        if tag in _STRUCTURAL_TAGS:
            self._check_nesting(tag, line)

        if tag in ("th", "td"):
            shot_id = attr_map.get("data-shot-id")
            start_ms = attr_map.get("data-start-ms")
            end_ms = attr_map.get("data-end-ms")
            if shot_id and start_ms is not None and end_ms is not None:
                self.shot_columns.append((shot_id, start_ms, end_ms, line))
        if tag == "td" and "data-field" in attr_map:
            self._check_cell(attr_map, line)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # 自闭合标签不参与嵌套栈，但仍需安全检查。
        line, _ = self.getpos()
        attr_map = {name: (value if value is not None else "") for name, value in attrs}
        self._check_security(tag, attr_map, line)

    def handle_endtag(self, tag: str) -> None:
        line, _ = self.getpos()
        if tag == "table" and self._table_stack:
            table = self._table_stack.pop()
            if not table["has_tbody"]:
                self.issues.append(_issue(
                    self.artifact, int(table["line"]), "table",
                    "table 缺少 tbody（tr 必须经 thead/tbody 包裹）",
                    expected="table/thead?/tbody/tr 嵌套", actual="无 tbody",
                ))
        if tag in _STRUCTURAL_TAGS and tag in self._stack:
            while self._stack:
                popped = self._stack.pop()
                if popped == tag:
                    break

    # ------------------------------------------------------------------
    # 各组检查
    # ------------------------------------------------------------------

    def _check_security(self, tag: str, attrs: dict[str, str], line: int) -> None:
        for name in ("href", "src"):
            value = attrs.get(name)
            if value and value.strip().lower().startswith("javascript:"):
                self.issues.append(_issue(
                    self.artifact, line, tag,
                    f"禁止 javascript: URL（{name}）",
                    expected="相对路径或内联内容", actual=value.strip()[:60],
                ))
        if tag == "script" and attrs.get("src") is not None:
            self.issues.append(_issue(
                self.artifact, line, tag,
                "禁止外链 <script src>；脚本必须内联",
                expected="内联 script", actual=attrs["src"][:60],
            ))
        if tag in _REMOTE_FORBIDDEN_TAGS:
            for name in ("src", "href"):
                value = attrs.get(name)
                if value and value.strip().lower().startswith(("http://", "https://")):
                    self.issues.append(_issue(
                        self.artifact, line, tag,
                        f"禁止 http(s) 外链资源（{tag}.{name}）",
                        expected="相对路径", actual=value.strip()[:60],
                    ))

    def _check_nesting(self, tag: str, line: int) -> None:
        if tag == "table":
            self._table_stack.append({"line": line, "has_tbody": False})
        elif tag == "tbody":
            if not self._table_stack:
                self.issues.append(_issue(
                    self.artifact, line, tag, "tbody 必须位于 table 内",
                    expected="table/tbody", actual="table 外",
                ))
            else:
                self._table_stack[-1]["has_tbody"] = True
        elif tag == "tr":
            nearest = next(
                (t for t in reversed(self._stack) if t in ("table", "thead", "tbody")),
                None,
            )
            if nearest is None:
                self.issues.append(_issue(
                    self.artifact, line, tag, "tr 必须位于 table 内",
                    expected="table/(thead|tbody)/tr", actual="table 外",
                ))
            elif nearest == "table":
                self.issues.append(_issue(
                    self.artifact, line, tag,
                    "tr 直接位于 table 下：缺少 tbody（或 thead）包裹",
                    expected="table/tbody/tr", actual="table/tr",
                ))
        elif tag in ("td", "th"):
            if "tr" not in self._stack:
                self.issues.append(_issue(
                    self.artifact, line, tag, f"{tag} 必须位于 tr 内",
                    expected="tr/" + tag, actual="tr 外",
                ))
        self._stack.append(tag)

    def _check_cell(self, attrs: dict[str, str], line: int) -> None:
        state = attrs.get("data-value-state")
        if state is None:
            self.issues.append(_issue(
                self.artifact, line, "td",
                f"分析单元格 {attrs['data-field']!r} 缺 data-value-state",
                expected="五态或 labelOnly", actual="缺失",
            ))
        elif state not in VALUE_STATES:
            self.issues.append(_issue(
                self.artifact, line, "td",
                f"非法 data-value-state：{state!r}",
                expected=sorted(VALUE_STATES), actual=state,
            ))
        elif state in FIVE_STATES:
            for required in ("data-confidence", "data-source", "data-verified"):
                if required not in attrs:
                    self.issues.append(_issue(
                        self.artifact, line, "td",
                        f"五态单元格 {attrs['data-field']!r} 缺 {required}",
                        expected=required, actual="缺失",
                    ))
            verified = attrs.get("data-verified")
            if verified is not None and verified not in ("true", "false"):
                self.issues.append(_issue(
                    self.artifact, line, "td",
                    "data-verified 只能是 true/false",
                    expected="true|false", actual=verified,
                ))
        refs = attrs.get("data-evidence-refs", "")
        # labelOnly 与 unknown 允许空 refs（docs/00 §4.4 豁免）。
        if refs.strip():
            for ref in refs.split():
                try:
                    parse_evidence_ref(ref)
                except EvidenceRefError as exc:
                    self.issues.append(_issue(
                        self.artifact, line, "td",
                        f"data-evidence-refs 条目非法：{exc.reason}",
                        expected="合法 evidenceRef", actual=ref,
                    ))
            shot_id = attrs.get("data-shot-id")
            if shot_id:
                self.evidence_cells[shot_id] = self.evidence_cells.get(shot_id, 0) + 1

    # ------------------------------------------------------------------
    # 文档级收尾检查
    # ------------------------------------------------------------------

    def finish(self) -> None:
        if self.html_attrs is None:
            self.issues.append(_issue(
                self.artifact, 1, "html", "缺少 <html> 根节点",
                expected="data-document-type 等 metadata", actual="无 <html>",
            ))
            return
        attrs, line = self.html_attrs, self.html_line

        doc_type = attrs.get("data-document-type")
        if doc_type is None:
            self.issues.append(_issue(
                self.artifact, line, "html", "缺 data-document-type",
                expected=sorted(DOCUMENT_TYPES), actual="缺失",
            ))
        elif doc_type not in DOCUMENT_TYPES:
            self.issues.append(_issue(
                self.artifact, line, "html",
                f"非法 data-document-type：{doc_type!r}",
                expected=sorted(DOCUMENT_TYPES), actual=doc_type,
            ))
        status = attrs.get("data-document-status")
        if status is None:
            self.issues.append(_issue(
                self.artifact, line, "html", "缺 data-document-status",
                expected=sorted(DOCUMENT_STATUSES), actual="缺失",
            ))
        elif status not in DOCUMENT_STATUSES:
            self.issues.append(_issue(
                self.artifact, line, "html",
                f"非法 data-document-status：{status!r}",
                expected=sorted(DOCUMENT_STATUSES), actual=status,
            ))
        for required in ("data-contract-version", "data-source-revision"):
            if not attrs.get(required):
                self.issues.append(_issue(
                    self.artifact, line, "html", f"缺必需 metadata：{required}",
                    expected="非空", actual=attrs.get(required, "缺失") or "缺失",
                ))

        if doc_type == "shotAnalysis":
            for block_line in self.story_block_lines:
                self.issues.append(_issue(
                    self.artifact, block_line, "section",
                    "story-block 元素不得出现在 shotAnalysis 文档",
                    expected="storyAnalysis 文档", actual="shotAnalysis",
                ))
            if not self.shot_columns:
                self.issues.append(_issue(
                    self.artifact, line, "html",
                    "shotAnalysis 必须包含至少一个镜头列"
                    "（带 data-shot-id/data-start-ms/data-end-ms 的 th/td 列头）",
                    expected=">= 1 个镜头列", actual="0",
                ))
            for shot_id, _, _, col_line in self.shot_columns:
                if self.evidence_cells.get(shot_id, 0) == 0:
                    self.issues.append(_issue(
                        self.artifact, col_line, "th",
                        f"镜头 {shot_id} 没有任何带非空 data-evidence-refs 的单元格",
                        expected="每镜头至少一个可追溯证据列", actual="0",
                    ))


# ---------------------------------------------------------------------------
# 严格模式：与 raw JSON 对齐（docs/04 §8.4）
# ---------------------------------------------------------------------------


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _check_strict(
    checker: _Checker,
    root: Path,
    issues: list[ValidationIssue],
) -> None:
    artifact = checker.artifact
    html_line = checker.html_line

    shots_path = root / "raw" / "shots.json"
    media_path = root / "raw" / "media.json"
    try:
        shots_doc = read_json(shots_path)
    except ContractError:
        issues.append(_issue(
            artifact, html_line, "html",
            "strict 模式需要可读的 raw/shots.json",
            expected=str(shots_path), actual="missing/unreadable",
        ))
        return
    try:
        media_doc = read_json(media_path)
    except ContractError:
        issues.append(_issue(
            artifact, html_line, "html",
            "strict 模式需要可读的 raw/media.json",
            expected=str(media_path), actual="missing/unreadable",
        ))
        return

    expected_shots: dict[str, dict] = {}
    shots = shots_doc.get("shots")
    if isinstance(shots, list):
        for entry in shots:
            if isinstance(entry, dict) and isinstance(entry.get("shotID"), str):
                expected_shots[entry["shotID"]] = entry

    page_ids = {shot_id for shot_id, _, _, _ in checker.shot_columns}
    if page_ids != set(expected_shots):
        issues.append(_issue(
            artifact, html_line, "html",
            "页面镜头列 shotID 集合与 raw/shots.json 不一致",
            expected=sorted(expected_shots), actual=sorted(page_ids),
        ))
    for shot_id, start_ms, end_ms, col_line in checker.shot_columns:
        entry = expected_shots.get(shot_id)
        if entry is None:
            continue
        expected_start = _as_int(entry.get("finalStartMs"))
        expected_end = _as_int(entry.get("finalEndMs"))
        actual_start, actual_end = _as_int_safe(start_ms), _as_int_safe(end_ms)
        if expected_start is not None and actual_start != expected_start:
            issues.append(_issue(
                artifact, col_line, "th",
                f"镜头 {shot_id} 的 data-start-ms 与 finalStartMs 不一致",
                expected=str(expected_start), actual=start_ms,
            ))
        if expected_end is not None and actual_end != expected_end:
            issues.append(_issue(
                artifact, col_line, "th",
                f"镜头 {shot_id} 的 data-end-ms 与 finalEndMs 不一致",
                expected=str(expected_end), actual=end_ms,
            ))

    revision = media_doc.get("source", {}).get("revisionID")
    page_revision = (checker.html_attrs or {}).get("data-source-revision")
    if isinstance(revision, str) and page_revision and page_revision != revision:
        issues.append(_issue(
            artifact, html_line, "html",
            "data-source-revision 与 media.json 的 revisionID 不一致",
            expected=revision, actual=page_revision,
        ))


def _as_int_safe(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def validate_html(
    path: Path, *, root: Path | None = None, strict: bool = False
) -> list[ValidationIssue]:
    """校验单个 shot/story analysis HTML 文件，返回全部 issue（不抛异常）。

    - 始终执行 §8.1 结构、§8.2 单元格、§8.3 安全检查；
    - ``strict=True`` 且提供 ``root``（output-dir）时追加 §8.4 数据一致性检查；
    - ``strict=True`` 但无 ``root`` 时记 warning 并跳过一致性检查。
    """
    path = Path(path)
    artifact = path.name
    checker = _Checker(artifact)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [
            _issue(artifact, 1, "html", f"HTML 文件不可读：{exc}",
                   expected="UTF-8 HTML", actual=str(exc))
        ]
    checker.feed(text)
    checker.close()
    checker.finish()
    issues = list(checker.issues)

    if strict:
        if root is None:
            issues.append(_issue(
                artifact, checker.html_line, "html",
                "strict 模式未提供 root，跳过数据一致性检查",
                expected="output-dir root", actual="None", severity="warning",
            ))
        else:
            _check_strict(checker, Path(root), issues)
    return issues
