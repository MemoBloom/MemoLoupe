"""review server 集成测试：后台线程起真实 HTTP 服务，urllib 打端点（docs/04 §5.1）。

覆盖：
- GET / 提供重渲染后的 shot-analysis.html；
- GET /story-analysis.html 提供重渲染后的 story-analysis.html；
- 路径防逃逸（编码变体 / 越权目录）；
- POST /api/corrections 合法/非法两条路径；
- POST /api/confirm 前置不满足时 400 带 reasons。
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from memoloupe.render.review_server import make_review_handler
from memoloupe.render.shot_html import render_shot_html
from memoloupe.render.story_html import render_story_html

FIXTURE_FULL = Path(__file__).parent.parent / "fixtures" / "output_full"

#: 不走系统代理（macOS 系统代理可能拦截 localhost 请求）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _request(
    base: str, path: str, *, method: str = "GET", payload: object = None
) -> tuple[int, str]:
    data = None
    headers = {}
    if payload is not None:
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _request_json(
    base: str, path: str, payload: object
) -> tuple[int, dict]:
    status, text = _request(base, path, method="POST", payload=payload)
    return status, json.loads(text)


@pytest.fixture
def review_env(tmp_path):
    """拷贝 output_full fixture，预渲染 HTML，后台线程起 server（port=0）。"""
    out_dir = tmp_path / "out"
    shutil.copytree(FIXTURE_FULL, out_dir)
    render_shot_html(out_dir, server_mode=True)
    render_story_html(out_dir, server_mode=True)
    handler = make_review_handler(out_dir)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield out_dir, base
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _framing_cell(html_text: str) -> str:
    match = re.search(
        r'<td data-field="visual\.framing" data-shot-id="SH0001"[^>]*>', html_text
    )
    assert match is not None, "页面缺少 SH0001 visual.framing 单元格"
    return match.group(0)


class TestGet:
    def test_index_serves_shot_analysis_html(self, review_env):
        _, base = review_env
        status, body = _request(base, "/")
        assert status == 200
        assert 'data-document-type="shotAnalysis"' in body

    def test_shot_analysis_html_by_name(self, review_env):
        _, base = review_env
        status, body = _request(base, "/shot-analysis.html")
        assert status == 200
        assert 'data-document-type="shotAnalysis"' in body

    def test_story_analysis_html_by_name(self, review_env):
        _, base = review_env
        status, body = _request(base, "/story-analysis.html")
        assert status == 200
        assert 'data-document-type="storyAnalysis"' in body

    def test_raw_file_served_read_only(self, review_env):
        _, base = review_env
        status, body = _request(base, "/raw/media.json")
        assert status == 200
        assert json.loads(body)["source"]["revisionID"]

    def test_path_escape_rejected(self, review_env):
        _, base = review_env
        for path in (
            "/%2e%2e/%2e%2e/etc/hosts",
            "/raw/%2e%2e/%2e%2e/%2e%2e/etc/hosts",
        ):
            status, _ = _request(base, path)
            assert status in (400, 403), f"{path} -> {status}"

    def test_disallowed_subtree_rejected(self, review_env):
        """corrections/ 不在只读服务范围内。"""
        out_dir, base = review_env
        (out_dir / "corrections").mkdir(exist_ok=True)
        (out_dir / "corrections" / "shotAnalysis.json").write_text("{}", encoding="utf-8")
        status, _ = _request(base, "/corrections/shotAnalysis.json")
        assert status in (400, 403)

    def test_missing_file_404(self, review_env):
        _, base = review_env
        status, _ = _request(base, "/raw/nope.json")
        assert status == 404


class TestPostCorrections:
    _CHANGE = {
        "entityID": "SH0001",
        "field": "visual.framing",
        "oldValue": "全景",
        "newValue": "特写",
        "state": "value",
        "verified": True,
    }

    def test_valid_change_persisted_and_rerendered(self, review_env):
        out_dir, base = review_env
        status, body = _request_json(base, "/api/corrections", {"changes": [self._CHANGE]})
        assert status == 200
        assert body["ok"] is True
        assert body["status"] == "underReview"
        assert body["changeCount"] == 1

        corr_path = out_dir / "corrections" / "shotAnalysis.json"
        assert corr_path.is_file()
        corr = json.loads(corr_path.read_text(encoding="utf-8"))
        assert len(corr["changes"]) == 1
        assert corr["changes"][0]["actor"] == "human"
        assert corr["changes"][0]["changedAt"]

        # 保存后 GET / 应看到最新状态与人工修正单元格
        status, html_text = _request(base, "/")
        assert status == 200
        assert 'data-document-status="underReview"' in html_text
        cell = _framing_cell(html_text)
        assert 'data-source="human"' in cell
        assert 'data-original-value="全景"' in cell

    def test_bare_array_body_accepted(self, review_env):
        _, base = review_env
        status, body = _request_json(base, "/api/corrections", [self._CHANGE])
        assert status == 200
        assert body["ok"] is True

    def test_story_corrections_persist_to_story_document(self, review_env):
        out_dir, base = review_env
        change = {
            "entityID": "B0001",
            "field": "blockTitle",
            "oldValue": "开场",
            "newValue": "新的故事标题",
            "state": "value",
            "verified": True,
        }
        status, body = _request_json(
            base,
            "/api/corrections",
            {"documentType": "storyAnalysis", "changes": [change]},
        )
        assert status == 200
        assert body["ok"] is True
        assert body["status"] == "underReview"
        story_corr = out_dir / "corrections" / "storyAnalysis.json"
        assert story_corr.is_file()
        assert not (out_dir / "corrections" / "shotAnalysis.json").exists()
        corr = json.loads(story_corr.read_text(encoding="utf-8"))
        assert corr["documentType"] == "storyAnalysis"
        assert corr["changes"][0]["entityID"] == "B0001"

        status, html_text = _request(base, "/story-analysis.html")
        assert status == 200
        assert 'data-document-status="underReview"' in html_text
        assert "新的故事标题" in html_text

    def test_invalid_state_rejected_without_write(self, review_env):
        out_dir, base = review_env
        bad = dict(self._CHANGE, state="bogus")
        status, body = _request_json(base, "/api/corrections", {"changes": [bad]})
        assert status == 400
        assert body["ok"] is False
        assert body["errors"]
        assert not (out_dir / "corrections" / "shotAnalysis.json").exists()

    def test_missing_required_field_rejected(self, review_env):
        out_dir, base = review_env
        bad = {"entityID": "SH0001", "state": "value", "verified": True}
        status, body = _request_json(base, "/api/corrections", {"changes": [bad]})
        assert status == 400
        assert body["ok"] is False
        assert any("field" in e for e in body["errors"])
        assert not (out_dir / "corrections" / "shotAnalysis.json").exists()

    def test_invalid_json_body_400(self, review_env):
        _, base = review_env
        status, body = _request(
            base, "/api/corrections", method="POST", payload=b"{not json"
        )
        assert status == 400
        assert json.loads(body)["ok"] is False


class TestConfirm:
    def test_confirm_rejected_when_completion_unmet(self, review_env):
        """fixture 含未核实的 absent-claimed，completion 不满足 → 400 + reasons。"""
        _, base = review_env
        status, body = _request_json(base, "/api/confirm", {"documentType": "shotAnalysis"})
        assert status == 400
        assert body["ok"] is False
        assert body["reasons"]


class TestReviewCommand:
    """``memoloupe review`` CLI 错误路径（server 本体由上面的真实 HTTP 用例覆盖）。"""

    def test_missing_output_dir_exit3(self, tmp_path, capsys):
        from memoloupe.cli.main import EXIT_INPUT, main

        code = main(["review", "--output-dir", str(tmp_path / "nope")])
        assert code == EXIT_INPUT
        assert "不存在" in capsys.readouterr().err

    def test_render_failure_exit3(self, tmp_path, capsys):
        """output-dir 存在但 raw/shots.json 缺失 → 渲染失败 → 退出码 3。"""
        from memoloupe.cli.main import EXIT_INPUT, main

        empty = tmp_path / "empty"
        empty.mkdir()
        code = main(["review", "--output-dir", str(empty)])
        assert code == EXIT_INPUT
        assert "渲染" in capsys.readouterr().err
