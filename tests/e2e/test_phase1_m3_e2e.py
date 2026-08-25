"""Phase 1 M3 端到端：人工修正 overlay（离线导入 + localhost review server）。

场景（类内按定义顺序执行，共享一次 mock 全服务跑通的 out_dir）：

1. 自定义 mock（visual.framing/content 给具体值，其余“无”）跑通 Phase 1，
   strict 校验 0 error；
2. ``memoloupe import-corrections`` 导入 SH0001 visual.framing 修正
   （全景 → 特写，verified=true）→ 单元格 data-source="human"、
   data-original-value 为旧值；
3. 同配置重跑 ``run_shot_analysis``（缓存命中重渲染）→ 修正仍在；
4. 不同内容的源视频跑到新 output-dir 并拷入旧 corrections → 状态 outdated、
   旧修正不应用；
5. 按 rules/completion.json 把 requireVerifiedStates 核实齐后经 review server
   POST /api/corrections + /api/confirm → 状态 confirmed → strict 0 error。
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

from memoloupe.analysis.observations import ValueState
from memoloupe.analysis.resolvers import DEFAULT_RESOLVERS, build_observations
from memoloupe.analysis.shot_pipeline import ShotAnalysisPipeline, ShotAnalysisRequest
from memoloupe.cli.main import EXIT_OK, main
from memoloupe.core.atomic_io import read_json
from memoloupe.core.config import load_config
from memoloupe.render.corrections import apply_corrections, load_corrections
from memoloupe.render.review_server import make_review_handler
from memoloupe.render.shot_html import RAW_FILES, render_shot_html
from memoloupe.services.mock import GROUP_OWNED_SECTIONS, MockUnifiedMediaService
from memoloupe.validate.cross_artifact import validate_output_dir
from memoloupe.validate.html_contract import validate_html

from conftest import E2E_SHOTS_ENV, synthesize_hardcut_video  # noqa: F401

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe 不在 PATH",
)


def _group_payload(group_name: str, shot_ids: list[str]) -> str:
    """与 default mock 同构，但 visual.framing/content 给具体值（便于修正断言）。"""
    shots = []
    for sid in shot_ids:
        shot: dict = {"shotID": sid}
        for section, fields in GROUP_OWNED_SECTIONS[group_name].items():
            if section == "confidence":
                shot["confidence"] = {name: "medium" for name in fields}
            elif section == "components":
                shot["components"] = {"texts": [], "compositingEvents": "无"}
            else:
                shot[section] = {name: "无" for name in fields}
        if group_name == "visual":
            shot["visual"]["framing"] = "全景"
            shot["visual"]["content"] = "机场出发画面"
        shots.append(shot)
    return json.dumps({"shots": shots}, ensure_ascii=False)


def _mock_run(video: Path, out_dir: Path) -> None:
    """自定义 mock 全服务跑通 Phase 1（同配置重跑应全部缓存命中）。"""
    config = load_config(env=dict(E2E_SHOTS_ENV))

    def respond(clips, group, call_index):
        return _group_payload(group.name, [c.shot_id for c in clips])

    report = ShotAnalysisPipeline().run(
        ShotAnalysisRequest(
            source=video,
            output_dir=out_dir,
            config=config,
            unified_service=MockUnifiedMediaService(respond),
        )
    )
    assert report.status == "complete", report.failures


def _read(out_dir: Path, name: str) -> dict:
    return read_json(out_dir / "raw" / f"{name}.json")


def _strict_errors(out_dir: Path) -> list:
    issues = list(validate_output_dir(out_dir, strict=True))
    html = out_dir / "shot-analysis.html"
    if html.is_file():
        issues.extend(validate_html(html, root=out_dir, strict=True))
    return [i for i in issues if i.severity == "error"]


def _framing_cell(html_text: str, shot_id: str = "SH0001") -> str:
    match = re.search(
        rf'<td data-field="visual\.framing" data-shot-id="{shot_id}"[^>]*>', html_text
    )
    assert match is not None
    return match.group(0)


def _verification_changes(out_dir: Path) -> list[dict]:
    """按 rules/completion.json 补齐 completion 缺口（应用现有 corrections 后）：

    - requiredFields 未解决（state 不在 value/absent）→ 人工给具体值并核实；
    - requireVerifiedStates（unmapped/absent-claimed）未核实 → 补 verified=true
      （state/newValue 保持原样）。
    """
    required_fields = frozenset({"visual.content", "visual.framing", "audio.speech"})
    resolved = frozenset({ValueState.VALUE, ValueState.ABSENT})
    raws: dict[str, dict | None] = {}
    for name in RAW_FILES:
        try:
            raws[name] = read_json(out_dir / "raw" / f"{name}.json")
        except Exception:
            raws[name] = None
    revision = raws["media"]["source"]["revisionID"]
    corrections = load_corrections(out_dir, "shotAnalysis")
    changes: list[dict] = []
    for shot in raws["shots"]["shots"]:
        shot_id = shot["shotID"]
        observations, _ = apply_corrections(
            build_observations(shot_id, raws, DEFAULT_RESOLVERS), corrections, revision
        )
        for obs in observations:
            if obs.field in required_fields and obs.state not in resolved:
                changes.append(
                    {
                        "entityID": shot_id,
                        "field": obs.field,
                        "oldValue": obs.original_value
                        if obs.original_value is not None
                        else obs.value,
                        "newValue": "人工核实补录",
                        "state": "value",
                        "verified": True,
                    }
                )
            elif obs.state in (ValueState.UNMAPPED, ValueState.ABSENT_CLAIMED) and not obs.verified:
                changes.append(
                    {
                        "entityID": shot_id,
                        "field": obs.field,
                        "oldValue": obs.original_value
                        if obs.original_value is not None
                        else obs.value,
                        "newValue": obs.value,
                        "state": obs.state.value,
                        "verified": True,
                    }
                )
    return changes


#: 不走系统代理（macOS 系统代理可能拦截 localhost 请求）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post(base: str, path: str, payload: object) -> tuple[int, dict]:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _OPENER.open(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


@pytest.fixture(scope="module")
def m3_env(tmp_path_factory):
    """模块级共享：自定义 mock 全服务跑通一次（类内用例按顺序演进同一 out_dir）。"""
    base = tmp_path_factory.mktemp("e2e-m3")
    video = synthesize_hardcut_video(base / "video.mp4")
    out_dir = base / "out"
    _mock_run(video, out_dir)
    return video, out_dir


class TestM3CorrectionsE2E:

    def test_1_mock_run_complete_and_strict_clean(self, m3_env):
        _, out_dir = m3_env
        shots = _read(out_dir, "shots")
        assert len(shots["shots"]) == 3
        assert _read(out_dir, "unified-media")["status"] == "complete"
        assert _strict_errors(out_dir) == []
        html = (out_dir / "shot-analysis.html").read_text(encoding="utf-8")
        cell = _framing_cell(html)
        assert 'data-source="unifiedModel"' in cell
        assert 'data-value-state="value"' in cell

    def test_2_import_correction_applies(self, m3_env, tmp_path):
        _, out_dir = m3_env
        revision = _read(out_dir, "media")["source"]["revisionID"]
        export = tmp_path / "corrections-export.json"
        export.write_text(
            json.dumps(
                {
                    "correctionVersion": 1,
                    "documentType": "shotAnalysis",
                    "sourceRevisionID": revision,
                    "changes": [
                        {
                            "entityID": "SH0001",
                            "field": "visual.framing",
                            "oldValue": "全景",
                            "newValue": "特写",
                            "state": "value",
                            "verified": True,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        assert main(["import-corrections", str(export), "--output-dir", str(out_dir)]) == EXIT_OK

        html = (out_dir / "shot-analysis.html").read_text(encoding="utf-8")
        cell = _framing_cell(html)
        assert 'data-source="human"' in cell
        assert 'data-original-value="全景"' in cell
        assert 'data-document-status="underReview"' in html

    def test_3_rerun_same_config_keeps_correction(self, m3_env):
        video, out_dir = m3_env
        _mock_run(video, out_dir)  # 缓存全命中重渲染
        html = (out_dir / "shot-analysis.html").read_text(encoding="utf-8")
        cell = _framing_cell(html)
        assert 'data-source="human"' in cell
        assert 'data-original-value="全景"' in cell

    def test_4_new_revision_marks_outdated(self, m3_env, tmp_path):
        _, out_dir = m3_env
        other = synthesize_hardcut_video(
            tmp_path / "other.mp4", colors=("blue", "red", "white")
        )
        out2 = tmp_path / "out2"
        _mock_run(other, out2)
        assert (
            _read(out2, "media")["source"]["revisionID"]
            != _read(out_dir, "media")["source"]["revisionID"]
        )

        # 旧 corrections 拷入新 output-dir：revision 不匹配 → outdated 且不应用
        (out2 / "corrections").mkdir()
        shutil.copy(
            out_dir / "corrections" / "shotAnalysis.json",
            out2 / "corrections" / "shotAnalysis.json",
        )
        render_shot_html(out2)
        html = (out2 / "shot-analysis.html").read_text(encoding="utf-8")
        assert 'data-document-status="outdated"' in html
        cell = _framing_cell(html)
        assert 'data-source="human"' not in cell
        assert 'data-source="unifiedModel"' in cell

    def test_5_review_server_confirm_flow(self, m3_env):
        _, out_dir = m3_env
        handler = make_review_handler(out_dir)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            changes = _verification_changes(out_dir)
            assert changes, "mock 全“无”响应应产生待核实的 absent-claimed 观察"
            status, body = _post(base, "/api/corrections", {"changes": changes})
            assert status == 200, body
            assert body["ok"] is True

            status, body = _post(base, "/api/confirm", {})
            assert status == 200, body
            assert body["ok"] is True
            assert body["status"] == "confirmed"
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

        corrections = load_corrections(out_dir, "shotAnalysis")
        assert corrections.confirmed_at is not None
        html = (out_dir / "shot-analysis.html").read_text(encoding="utf-8")
        assert 'data-document-status="confirmed"' in html
        assert _strict_errors(out_dir) == []
