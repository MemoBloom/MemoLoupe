"""render.review_server — localhost 人工校对 review server（docs/04 §5.1 模式 2）。

- 仅用 stdlib ``http.server``，只绑 127.0.0.1；
- GET 提供 output-dir 静态文件只读服务：``shot-analysis.html``、
  ``story-analysis.html`` 与
  ``clips/``、``evidence/``、``raw/``；路径 resolve 后必须留在 out_dir 内，
  否则 400/403；
- 每次 GET ``/``、``/shot-analysis.html`` 或 ``/story-analysis.html`` 前以
  ``server_mode=True`` 重渲染（保证页面看到最新 corrections）；重渲染失败
  回退磁盘现有文件并记 stderr；
- ``POST /api/corrections`` 接受 ``{"changes": [...]}`` 或直接数组，逐项校验
  后经 :func:`memoloupe.render.corrections.append_changes` 原子落盘并重渲染；
- ``POST /api/confirm`` 走 :func:`memoloupe.analysis.completion.confirm_document`
  三道闸门，成功后重渲染为 confirmed；
- 日志只写 stderr，POST body 预览截断 200 字符；
- :func:`run_review_server` 阻塞 serve，KeyboardInterrupt/EOF 优雅关闭。
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from memoloupe.analysis.completion import confirm_document
from memoloupe.analysis.observations import ValueState
from memoloupe.core.atomic_io import read_json
from memoloupe.core.errors import ContractError
from memoloupe.render.corrections import (
    append_changes,
    document_status,
    load_corrections,
)
from memoloupe.render.shot_html import render_shot_html
from memoloupe.render.story_html import render_story_html

DEFAULT_DOCUMENT_TYPE = "shotAnalysis"
DOCUMENT_TYPES = frozenset({"shotAnalysis", "storyAnalysis"})

#: 只读静态服务允许的顶层目录/文件。
_ALLOWED_TOP = frozenset({
    "clips",
    "evidence",
    "raw",
    "shot-analysis.html",
    "story-analysis.html",
})

#: POST body 上限（10 MiB，防内存耗尽）。
MAX_BODY_BYTES = 10 * 1024 * 1024

#: 日志中 body 预览的最大字符数。
LOG_BODY_PREVIEW = 200

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".mp4": "video/mp4",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def current_revision(out_dir: Path) -> str:
    """当前源 revision（raw/media.json 缺失或字段非法时为空串）。"""
    try:
        media = read_json(Path(out_dir) / "raw" / "media.json")
    except ContractError:
        return ""
    value = media.get("source", {}).get("revisionID")
    return value if isinstance(value, str) else ""


def validate_change(change: object, index: int) -> list[str]:
    """逐项校验一条修正的必要字段（entityID/field/state/verified），返回错误列表。"""
    if not isinstance(change, dict):
        return [f"changes[{index}] 必须是对象"]
    errors: list[str] = []
    for key in ("entityID", "field"):
        value = change.get(key)
        if not isinstance(value, str) or not value:
            errors.append(f"changes[{index}].{key} 必须是非空字符串")
    state = change.get("state")
    if state not in {s.value for s in ValueState}:
        errors.append(
            f"changes[{index}].state 非法：{state!r}（应为 "
            f"{sorted(s.value for s in ValueState)} 之一）"
        )
    if not isinstance(change.get("verified"), bool):
        errors.append(f"changes[{index}].verified 必须是布尔值")
    kind = change.get("kind", "field")
    if kind not in ("field", "boundary"):
        errors.append(f"changes[{index}].kind 非法：{kind!r}")
    return errors


def normalize_changes(changes: list[dict]) -> list[dict]:
    """补默认 oldValue/newValue（schema 必填、语义上可为 null）。"""
    normalized: list[dict] = []
    for change in changes:
        entry = dict(change)
        entry.setdefault("oldValue", None)
        entry.setdefault("newValue", None)
        normalized.append(entry)
    return normalized


def make_review_handler(out_dir: Path) -> type[BaseHTTPRequestHandler]:
    """构造绑定 ``out_dir`` 的 handler 类（工厂形式便于测试注入）。"""
    root = Path(out_dir).resolve()

    def _rerender_shot() -> None:
        """server_mode 重渲染；失败回退磁盘现有文件并记 stderr。"""
        try:
            render_shot_html(root, server_mode=True)
        except Exception as exc:  # 渲染失败不应击垮只读服务
            print(f"review-server：重渲染失败，回退磁盘现有文件：{exc}", file=sys.stderr)

    def _rerender_story() -> None:
        """server_mode 重渲染 story；失败回退磁盘现有文件并记 stderr。"""
        try:
            render_story_html(root, server_mode=True)
        except Exception as exc:  # 渲染失败不应击垮只读服务
            print(
                f"review-server：story 重渲染失败，回退磁盘现有文件：{exc}",
                file=sys.stderr,
            )

    def _rerender_document(document_type: str) -> None:
        if document_type == "storyAnalysis":
            _rerender_story()
        else:
            _rerender_shot()

    class ReviewHandler(BaseHTTPRequestHandler):
        server_version = "MemoLoupeReview/0.1"

        # ---- 日志：只写 stderr ----
        def log_message(self, format: str, *args: object) -> None:
            print(
                f"review-server {self.address_string()} {format % args}",
                file=sys.stderr,
            )

        def _log_body_preview(self, raw: bytes) -> None:
            preview = raw[:LOG_BODY_PREVIEW].decode("utf-8", errors="replace")
            if len(raw) > LOG_BODY_PREVIEW:
                preview += "…<截断>"
            print(f"review-server POST {self.path} body 预览: {preview}", file=sys.stderr)

        # ---- 响应助手 ----
        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path) -> None:
            body = path.read_bytes()
            content_type = _CONTENT_TYPES.get(
                path.suffix.lower(), "application/octet-stream"
            )
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ---- GET ----
        def do_GET(self) -> None:
            rel = unquote(urlsplit(self.path).path)
            if rel in ("/", "/shot-analysis.html"):
                _rerender_shot()
                target = root / "shot-analysis.html"
                if not target.is_file():
                    self._send_json(
                        404, {"ok": False, "errors": ["shot-analysis.html 不存在"]}
                    )
                    return
                self._send_file(target)
                return
            if rel == "/story-analysis.html":
                _rerender_story()
                target = root / "story-analysis.html"
                if not target.is_file():
                    self._send_json(
                        404, {"ok": False, "errors": ["story-analysis.html 不存在"]}
                    )
                    return
                self._send_file(target)
                return
            self._serve_static(rel)

        def _serve_static(self, rel: str) -> None:
            candidate = (root / rel.lstrip("/")).resolve()
            if not (candidate != root and root in candidate.parents):
                self._send_json(
                    403, {"ok": False, "errors": ["路径越出 output-dir，已拒绝"]}
                )
                return
            top = candidate.relative_to(root).parts[0]
            if top not in _ALLOWED_TOP:
                self._send_json(
                    403, {"ok": False, "errors": [f"不允许访问 {top!r}（只读范围外）"]}
                )
                return
            if not candidate.is_file():
                self._send_json(404, {"ok": False, "errors": [f"文件不存在：{rel}"]})
                return
            self._send_file(candidate)

        # ---- POST ----
        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path not in ("/api/corrections", "/api/confirm"):
                self._send_json(404, {"ok": False, "errors": [f"未知端点：{path}"]})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._send_json(400, {"ok": False, "errors": ["Content-Length 非法"]})
                return
            if length > MAX_BODY_BYTES:
                self._send_json(413, {"ok": False, "errors": ["body 超过大小上限"]})
                return
            raw = self.rfile.read(length) if length else b""
            self._log_body_preview(raw)
            if path == "/api/corrections":
                self._post_corrections(raw)
            else:
                self._post_confirm(raw)

        def _post_corrections(self, raw: bytes) -> None:
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, {"ok": False, "errors": ["body 不是合法 JSON"]})
                return
            revision: str | None = None
            document_type = DEFAULT_DOCUMENT_TYPE
            if isinstance(payload, list):
                changes = payload
            elif isinstance(payload, dict):
                changes = payload.get("changes")
                value = payload.get("sourceRevisionID")
                if isinstance(value, str) and value:
                    revision = value
                requested_type = payload.get("documentType")
                if isinstance(requested_type, str) and requested_type:
                    document_type = requested_type
            else:
                self._send_json(
                    400,
                    {"ok": False, "errors": ["body 必须是 {\"changes\": [...]} 或修正数组"]},
                )
                return
            if document_type not in DOCUMENT_TYPES:
                self._send_json(
                    400,
                    {"ok": False, "errors": [f"documentType 非法：{document_type!r}"]},
                )
                return
            if not isinstance(changes, list) or not changes:
                self._send_json(
                    400, {"ok": False, "errors": ["changes 必须是非空数组"]}
                )
                return
            errors: list[str] = []
            for index, change in enumerate(changes):
                errors.extend(validate_change(change, index))
            if errors:
                self._send_json(400, {"ok": False, "errors": errors})
                return
            source_revision = (
                revision if revision is not None else current_revision(root)
            )
            try:
                result = append_changes(
                    root, document_type, source_revision, normalize_changes(changes)
                )
            except ContractError as exc:
                self._send_json(400, {"ok": False, "errors": [str(exc)]})
                return
            _rerender_document(document_type)
            self._send_json(
                200,
                {
                    "ok": True,
                    "status": document_status(
                        load_corrections(root, document_type), current_revision(root)
                    ),
                    "changeCount": len(result.changes),
                },
            )

        def _post_confirm(self, raw: bytes) -> None:
            document_type = DEFAULT_DOCUMENT_TYPE
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, {"ok": False, "errors": ["body 不是合法 JSON"]})
                return
            if isinstance(payload, dict):
                requested_type = payload.get("documentType")
                if isinstance(requested_type, str) and requested_type:
                    document_type = requested_type
            if document_type not in DOCUMENT_TYPES:
                self._send_json(
                    400,
                    {"ok": False, "errors": [f"documentType 非法：{document_type!r}"]},
                )
                return
            ok, reasons = confirm_document(root, document_type)
            if not ok:
                self._send_json(400, {"ok": False, "reasons": reasons})
                return
            _rerender_document(document_type)
            self._send_json(200, {"ok": True, "status": "confirmed"})

    return ReviewHandler


def run_review_server(out_dir: Path, port: int = 8765) -> None:
    """阻塞 serve（仅 127.0.0.1）；KeyboardInterrupt/EOF 优雅关闭。"""
    root = Path(out_dir).resolve()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_review_handler(root))
    server.daemon_threads = True
    actual_port = server.server_address[1]
    print(
        f"review server 已启动：http://127.0.0.1:{actual_port}/"
        "（仅本机访问，Ctrl-C 退出）",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except (KeyboardInterrupt, EOFError):
        print("review server 已关闭", file=sys.stderr)
    finally:
        server.server_close()
