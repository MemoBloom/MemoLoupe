"""render.shot_html 单元测试：对 fixture output-dir 渲染并回验 HTML 契约。

本阶段无模型产物时模型字段落 unknown；模型产物存在且成功时按 resolver
结果渲染 value/absent-claimed，且所有模型原文必须 HTML escape。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from memoloupe.render.shot_html import SHOT_RENDER_VERSION, render_shot_html
from memoloupe.validate.html_contract import validate_html

FIXTURE_FULL = Path(__file__).parent.parent / "fixtures" / "output_full"


def _copy_fixture(tmp_path: Path) -> Path:
    work = tmp_path / "out"
    shutil.copytree(FIXTURE_FULL, work)
    return work


def _errors(path: Path, work: Path):
    return [i for i in validate_html(path, root=work, strict=True) if i.severity == "error"]


class TestRenderWithoutModelArtifacts:
    """本阶段形态：无 unified-media.json，模型字段全部 unknown。"""

    def test_render_validates_clean_and_covers_all_shots(self, tmp_path):
        work = _copy_fixture(tmp_path)
        (work / "raw" / "unified-media.json").unlink()
        out = render_shot_html(work)
        assert out == work / "shot-analysis.html"
        assert _errors(out, work) == []

        html = out.read_text(encoding="utf-8")
        # 全部 3 个镜头列。
        for shot_id in ("SH0001", "SH0002", "SH0003"):
            assert f'scope="col" data-shot-id="{shot_id}"' in html
        # 模型字段 state=unknown。
        assert (
            'data-field="visual.content" data-shot-id="SH0001" '
            'data-value-state="unknown"' in html
        )
        assert (
            'data-field="editing.transition" data-shot-id="SH0003" '
            'data-value-state="unknown"' in html
        )
        # 确定性字段按 resolver 结果渲染。
        assert (
            'data-field="audio.bgmPresence" data-shot-id="SH0002" '
            'data-value-state="absent"' in html
        )
        assert (
            'data-field="audio.speech" data-shot-id="SH0001" '
            'data-value-state="value"' in html
        )
        # absent 与 unknown 可见文案不同。
        assert "无（确定性检测）" in html
        assert "未知" in html
        # confidence=unknown 不隐藏。
        assert "置信度 unknown" in html

    def test_missing_clips_disable_play_buttons(self, tmp_path):
        work = _copy_fixture(tmp_path)
        assert not (work / "clips").exists()
        html = render_shot_html(work).read_text(encoding="utf-8")
        assert 'data-clip-src="clips/' not in html
        assert html.count("play-btn") >= 3
        assert "disabled" in html

    def test_snapshot_root_and_cell(self, tmp_path):
        work = _copy_fixture(tmp_path)
        (work / "raw" / "unified-media.json").unlink()
        html = render_shot_html(work).read_text(encoding="utf-8")
        lines = html.splitlines()
        html_line = next(line for line in lines if line.startswith("<html "))
        assert html_line == (
            '<html lang="zh-CN" data-document-type="shotAnalysis" '
            'data-document-status="draft" data-contract-version="1.0" '
            'data-source-revision="a1b2c3d4e5f6">'
        )
        # 锁一个确定性单元格片段（属性顺序固定）。
        assert (
            '<td data-field="audio.energy" data-shot-id="SH0001" '
            'data-value-state="value" data-confidence="high" '
            'data-evidence-refs="raw/audio-energy.json#shots[0]" '
            'data-source="ffmpeg" data-verified="false">' in html
        )

    def test_status_goes_to_root_and_metadata(self, tmp_path):
        work = _copy_fixture(tmp_path)
        html = render_shot_html(work, status="underReview").read_text(encoding="utf-8")
        assert 'data-document-status="underReview"' in html
        assert "<dd>underReview</dd>" in html


class TestRenderWithModelArtifacts:
    """模型产物存在且成功：value / absent-claimed 路径与 escape。"""

    def test_model_cells_render_values(self, tmp_path):
        work = _copy_fixture(tmp_path)
        out = render_shot_html(work)
        assert _errors(out, work) == []
        html = out.read_text(encoding="utf-8")
        assert (
            'data-field="visual.content" data-shot-id="SH0001" '
            'data-value-state="value"' in html
        )
        assert "机场出发画面" in html
        # 模型声称“无” -> absent-claimed，与确定性 absent 文案不同。
        assert (
            'data-field="components.compositingEvents" data-shot-id="SH0001" '
            'data-value-state="absent-claimed"' in html
        )
        assert "模型声称无" in html

    def test_model_output_is_escaped(self, tmp_path):
        work = _copy_fixture(tmp_path)
        poison = '<script>alert("x")</script>\n"引号" <b>中文</b>'
        unified = work / "raw" / "unified-media.json"
        data = json.loads(unified.read_text(encoding="utf-8"))
        data["batches"][0]["response"]["shots"][0]["visual"]["content"] = poison
        unified.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        out = render_shot_html(work)
        html = out.read_text(encoding="utf-8")
        # 原文不得以可解析形式出现；escape 后的文本必须可见。
        assert poison not in html
        assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in html
        assert "引号" in html
        # script 标签数量不得因模型输出增加（模板自带内联 script 除外）。
        baseline_work = _copy_fixture(tmp_path / "baseline")
        baseline_html = render_shot_html(baseline_work).read_text(encoding="utf-8")
        assert html.count("<script") == baseline_html.count("<script")
        # 契约校验仍然零 error。
        assert _errors(out, work) == []


class TestResourcePolicy:
    def test_no_external_resources_and_csp_present(self, tmp_path):
        work = _copy_fixture(tmp_path)
        html = render_shot_html(work).read_text(encoding="utf-8")
        assert "<script src" not in html
        assert 'src="http' not in html and "href=\"http" not in html
        assert (
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            "img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; "
            'script-src \'unsafe-inline\'">' in html
        )
        # 代表帧使用相对路径。
        assert 'src="evidence/frames/F_SH0001_MAIN.jpg"' in html
        # needsReview 过滤控件与键盘可达的播放按钮。
        assert 'id="filter-needs-review"' in html
        assert "<button" in html
        assert SHOT_RENDER_VERSION == "render.v1"
        assert "render.v1" in html
