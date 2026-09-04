"""render.shot_html 单元测试：对 fixture output-dir 渲染并回验 HTML 契约。

本阶段无模型产物时模型字段落 unknown；模型产物存在且成功时按 resolver
结果渲染 value/absent-claimed，且所有模型原文必须 HTML escape。
"""

from __future__ import annotations

import html as html_lib
import json
import re
import shutil
import sys
import types
from dataclasses import replace as dc_replace
from pathlib import Path

import pytest

from memoloupe.analysis.observations import Source, ValueState
from memoloupe.render.shot_html import (
    SHOT_RENDER_VERSION,
    _assign_motion_lanes,
    render_shot_html,
)
from memoloupe.validate.html_contract import validate_html

FIXTURE_FULL = Path(__file__).parent.parent / "fixtures" / "output_full"


def _copy_fixture(tmp_path: Path) -> Path:
    work = tmp_path / "out"
    shutil.copytree(FIXTURE_FULL, work)
    return work


def _errors(path: Path, work: Path):
    return [i for i in validate_html(path, root=work, strict=True) if i.severity == "error"]


def _write_corrections_file(work: Path) -> None:
    corr_dir = work / "corrections"
    corr_dir.mkdir(exist_ok=True)
    (corr_dir / "shotAnalysis.json").write_text(
        json.dumps(
            {
                "correctionVersion": 1,
                "documentType": "shotAnalysis",
                "sourceRevisionID": "a1b2c3d4e5f6",
                "changes": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _install_fake_corrections(monkeypatch, *, status: str = "underReview", mutate=None):
    """注入 render.corrections 的冻结接口假实现（并行任务，真实模块可能不存在）。"""
    mod = types.ModuleType("memoloupe.render.corrections")
    mod.Corrections = dict
    mod.load_corrections = lambda out_dir, document_type: {"documentType": document_type}
    mod.document_status = lambda corrections, current_revision: status

    def _apply(observations, corrections, current_revision):
        if mutate is not None:
            return mutate(observations), ["fake-overlay-warning"]
        return list(observations), []

    mod.apply_corrections = _apply
    mod.boundary_changes = lambda corrections: []
    monkeypatch.setitem(sys.modules, "memoloupe.render.corrections", mod)
    return mod


def _correct_framing(observations):
    """把 SH0001 visual.framing 改为人工修正：全景 → 中景。"""
    out = []
    for obs in observations:
        if obs.field == "visual.framing" and obs.shot_id == "SH0001":
            obs = dc_replace(
                obs,
                value="中景",
                state=ValueState.VALUE,
                source=Source.HUMAN,
                verified=True,
                original_value="全景",
            )
        out.append(obs)
    return out


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
            'data-field="visual.contentSummary" data-shot-id="SH0001" '
            'data-value-state="unknown"' in html
        )
        assert (
            'data-field="function.shotTone" data-shot-id="SH0003" '
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
        # confidence=unknown 不隐藏，但 UI 显示为用户可读中文。
        assert "可信度待确认" in html

    def test_missing_clips_disable_play_buttons(self, tmp_path):
        work = _copy_fixture(tmp_path)
        assert not (work / "clips").exists()
        html = render_shot_html(work).read_text(encoding="utf-8")
        assert 'data-clip-src="clips/' not in html
        assert html.count("shot-jump") >= 3
        assert "disabled" in html
        assert 'id="play-full-video"' in html

    def test_snapshot_root_and_cell(self, tmp_path):
        work = _copy_fixture(tmp_path)
        (work / "raw" / "unified-media.json").unlink()
        html = render_shot_html(work).read_text(encoding="utf-8")
        lines = html.splitlines()
        html_line = next(line for line in lines if line.startswith("<html "))
        assert html_line == (
            '<html lang="zh-CN" data-document-type="shotAnalysis" '
            'data-document-status="draft" data-contract-version="1.0" '
            'data-source-revision="a1b2c3d4e5f6" data-render-version="render.v3">'
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
        assert "<dd>校对中</dd>" in html

    def test_logo_asset_copied_and_referenced(self, tmp_path):
        work = _copy_fixture(tmp_path)
        from memoloupe.render.shot_html import render_shot_html

        html_path = render_shot_html(work)
        document = html_path.read_text(encoding="utf-8")
        assert (work / "assets" / "memoloupe-logo.png").is_file()
        assert '<img class="brand-logo" src="assets/memoloupe-logo.png" alt="MemoLoupe"' in document

    def test_render_version_is_v3(self):
        from memoloupe.render.shot_html import SHOT_RENDER_VERSION

        assert SHOT_RENDER_VERSION == "render.v3"

    def test_light_brand_theme_tokens(self, tmp_path):
        work = _copy_fixture(tmp_path)
        from memoloupe.render.shot_html import render_shot_html

        document = render_shot_html(work).read_text(encoding="utf-8")
        assert "color-scheme: light" in document
        assert "--background: #f7f2e6" in document
        assert "--brand: #a57100" in document
        for dark_token in ("#09090b", "#111113", "#18181b", "#27272a", "#3f3f46"):
            assert dark_token not in document


class TestRenderWithModelArtifacts:
    """模型产物存在且成功：value / absent-claimed 路径与 escape。"""

    def test_model_cells_render_values(self, tmp_path):
        work = _copy_fixture(tmp_path)
        out = render_shot_html(work)
        assert _errors(out, work) == []
        html = out.read_text(encoding="utf-8")
        assert (
            'data-field="visual.contentSummary" data-shot-id="SH0001" '
            'data-value-state="value"' in html
        )
        assert "旅行者；拖行李走动；机场出发大厅；行李箱" in html
        # 模型声称“无” -> absent-claimed，与确定性 absent 文案不同。
        assert (
            'data-field="components.nonTextOverlayEvents" data-shot-id="SH0001" '
            'data-value-state="absent-claimed"' in html
        )
        assert "模型未发现" in html

    def test_model_output_is_escaped(self, tmp_path):
        work = _copy_fixture(tmp_path)
        poison = '<script>alert("x")</script>\n"引号" <b>中文</b>'
        unified = work / "raw" / "unified-media.json"
        data = json.loads(unified.read_text(encoding="utf-8"))
        data["batches"][0]["response"]["shots"][0]["visual"]["subjects"] = poison
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
        # needsReview 过滤控件、时间线跳转、完整视频入口与右侧分层详情。
        assert 'id="filter-needs-review"' in html
        assert "<button" in html
        assert 'id="play-full-video"' in html
        assert SHOT_RENDER_VERSION == "render.v3"
        assert "render.v3" in html
        assert 'id="shot-summary"' in html
        assert 'id="shot-timeline"' in html
        assert 'class="filmstrip"' in html
        assert "镜头详情" in html
        assert 'id="field-matrix-panel"' in html
        assert "默认收起" in html
        assert "inspector-hero" in html
        assert "快速判断" in html
        assert "详细信息" in html
        assert "只看异常" in html
        assert "待确认" in html
        assert "inspector-field-row" in html
        assert "inspector-field-summary" in html
        assert 'class="field-category-nav"' in html
        assert 'data-field-filter="core"' in html
        assert 'data-field-group="core"' in html
        assert 'data-field-group="visual-style"' in html
        assert "核心审片" in html
        assert "镜头内容摘要" in html
        assert 'id="story-layer"' not in html
        assert 'id="story-timeline-band"' in html
        assert 'id="motion-timeline-band"' in html
        assert "动效</span>" in html
        assert "镜头与故事时间线" in html
        assert "故事</span>" in html
        assert "机场出发" in html
        assert "旅程启动" in html
        assert 'class="story-segment shot-jump"' in html
        assert 'data-layer-id="B0001"' in html
        assert 'data-layer-shot-ids="SH0001 SH0002"' in html
        assert "story-shot-chip" not in html
        assert "<!--STORY_LAYER-->" not in html
        assert 'class="story-block"' not in html
        assert "var SHOT_INSPECTOR =" in html
        assert '"clipSrc"' in html
        assert '"frameRef"' in html
        assert '"story"' in html
        assert '"groups"' in html


class TestMotionEffectsRendering:
    def test_motion_effects_summary_and_sidebar_context_render(self, tmp_path):
        work = _copy_fixture(tmp_path)
        motion_path = work / "raw" / "motion-effects.json"
        motion = json.loads(motion_path.read_text(encoding="utf-8"))
        motion["status"] = "complete"
        motion["frameMetrics"] = [
            {
                "frameIndex": 1,
                "timeMs": 1500,
                "diff": 0.2,
                "motionEnergy": 0.3,
                "brightness": 0.6,
                "brightnessDelta": 0.2,
                "repeatScore": 0.2,
                "cutScore": 0.34,
                "dxPxSample": 3.0,
                "dyPxSample": 0.5,
                "scaleRatio": 1.0,
                "zoomScore": 0.0,
                "shakeScore": 3.5,
            }
        ]
        motion["speedRamps"] = [
            {
                "type": "impact_cut",
                "startMs": 1400,
                "endMs": 1525,
                "durationMs": 125,
                "avgMotion": 0.3,
                "confidence": "medium",
                "evidence": "cut_score peak=0.34",
                "replicationHint": "Use a 2-4 frame exposure hit.",
                "needsVisualConfirmation": True,
                "evidenceRefs": ["raw/motion-effects.json#frameMetrics[0]"],
            }
        ]
        motion["keyframeCandidates"] = [
            {
                "shotID": "SH0001",
                "timeMs": 1500,
                "property": "position",
                "inferredChange": {
                    "text": "Position approx (3.0px, 0.5px) in 96x54 sample"
                },
                "confidence": "high",
                "replicationHint": "Use position keyframes with easing.",
                "needsVisualConfirmation": True,
                "evidenceRefs": ["raw/motion-effects.json#frameMetrics[0]"],
            }
        ]
        motion["digest"]["items"] = [
            {
                "kind": "position",
                "timeRange": "1500 ms",
                "summary": "Position approx (3.0px, 0.5px) in 96x54 sample",
                "confidence": "high",
                "needsVisualConfirmation": True,
                "evidenceRefs": ["raw/motion-effects.json#frameMetrics[0]"],
            }
        ]
        motion["shots"][0]["candidateCount"] = 1
        motion["shots"][0]["properties"] = ["position"]
        motion["shots"][0]["needsReview"] = True
        motion_path.write_text(json.dumps(motion, ensure_ascii=False), encoding="utf-8")

        out = render_shot_html(work)
        assert _errors(out, work) == []
        html = out.read_text(encoding="utf-8")

        assert "运动复刻候选" in html
        assert "2 个候选 · 1 镜头" in html
        assert 'class="metric-card metric-card-motion"' in html
        assert 'id="motion-timeline-band"' in html
        assert 'class="motion-event shot-jump is-point"' in html
        assert 'class="motion-event shot-jump is-range is-compact"' in html
        assert "圆点 = 关键帧" in html
        # 点事件(1500ms)与 ramp(1400–1525ms) 在显示宽度上重叠 → 2 条泳道。
        assert "--motion-lanes: 2" in html
        assert "--motion-lane: 0" in html
        assert 'data-motion-kind="position"' in html
        assert 'data-motion-kind="impact_cut"' in html
        assert "动效</span>" in html
        assert '"motionEffects"' in html
        assert '"位移"' in html
        assert '"冲击卡点"' in html
        assert "Position approx (3.0px, 0.5px) in 96x54 sample" in html
        assert "raw/motion-effects.json#frameMetrics[0]" in html
        assert "需要人工视觉确认" in html
        assert "追溯依据" in html
        assert "is-motion" in html

    def test_overlapping_speed_ramps_render_on_separate_lanes(self, tmp_path):
        work = _copy_fixture(tmp_path)
        motion_path = work / "raw" / "motion-effects.json"
        motion = json.loads(motion_path.read_text(encoding="utf-8"))
        motion["status"] = "complete"
        ramp = {
            "type": "impact_cut",
            "durationMs": 400,
            "avgMotion": 0.3,
            "confidence": "medium",
            "evidence": "cut_score peak=0.34",
            "replicationHint": "Use a 2-4 frame exposure hit.",
            "needsVisualConfirmation": True,
            "evidenceRefs": ["raw/motion-effects.json#frameMetrics[0]"],
        }
        motion["speedRamps"] = [
            {**ramp, "startMs": 1000, "endMs": 1400},
            {**ramp, "startMs": 1200, "endMs": 1600},
        ]
        motion_path.write_text(json.dumps(motion, ensure_ascii=False), encoding="utf-8")

        out = render_shot_html(work)
        assert _errors(out, work) == []
        html = out.read_text(encoding="utf-8")

        assert "--motion-lanes: 2" in html
        assert "--motion-lane: 1" in html


class TestCorrectionsOverlay:
    """corrections overlay：raw → resolver → apply_corrections → HTML（docs/02 §6）。"""

    def test_corrected_cell_has_human_source_and_original_value(self, tmp_path, monkeypatch):
        work = _copy_fixture(tmp_path)
        _write_corrections_file(work)
        _install_fake_corrections(monkeypatch, mutate=_correct_framing)
        html = render_shot_html(work).read_text(encoding="utf-8")
        # 命中修正的单元格：source=human、verified=true、保留 escape 后的旧值。
        assert (
            '<td data-field="visual.framing" data-shot-id="SH0001" '
            'data-value-state="value"' in html
        )
        assert 'data-source="human" data-verified="true" data-original-value="全景"' in html
        # 新值可见。
        assert "中景" in html

    @pytest.mark.parametrize(
        "doc_status", ["draft", "underReview", "confirmed", "outdated"]
    )
    def test_document_status_comes_from_corrections(self, tmp_path, monkeypatch, doc_status):
        work = _copy_fixture(tmp_path)
        _write_corrections_file(work)
        _install_fake_corrections(monkeypatch, status=doc_status)
        html = render_shot_html(work).read_text(encoding="utf-8")
        assert f'data-document-status="{doc_status}"' in html
        labels = {
            "draft": "未校对",
            "underReview": "校对中",
            "confirmed": "已确认",
            "outdated": "需更新",
        }
        assert f"<dd>{labels[doc_status]}</dd>" in html

    def test_overlay_rendered_doc_validates_strict(self, tmp_path, monkeypatch):
        work = _copy_fixture(tmp_path)
        _write_corrections_file(work)
        _install_fake_corrections(monkeypatch, status="underReview", mutate=_correct_framing)
        out = render_shot_html(work)
        assert _errors(out, work) == []

    def test_overlay_warnings_surface_in_validation_block(self, tmp_path, monkeypatch):
        work = _copy_fixture(tmp_path)
        _write_corrections_file(work)
        _install_fake_corrections(monkeypatch, mutate=_correct_framing)
        html = render_shot_html(work).read_text(encoding="utf-8")
        assert "fake-overlay-warning" in html

    def test_no_corrections_file_falls_back_to_status_param(self, tmp_path):
        work = _copy_fixture(tmp_path)
        assert not (work / "corrections").exists()
        html = render_shot_html(work, status="underReview").read_text(encoding="utf-8")
        assert 'data-document-status="underReview"' in html


class TestEditingControls:
    """受控词表字段渲染 <select>，自由文本字段渲染 <input>（docs/04 §3.3/§5.2）。"""

    def test_vocab_field_renders_select_with_vocabulary_options(self, tmp_path):
        work = _copy_fixture(tmp_path)
        html = render_shot_html(work).read_text(encoding="utf-8")
        # visual.framing 是受控词表字段，fixture 中 SH0001 当前值为 全景。
        for value in ("远景", "全景", "中景", "近景", "特写", "unknown"):
            assert f'<option value="{value}"' in html
        assert '<option value="unknown">待确认</option>' in html
        assert '<option value="全景" selected>全景</option>' in html
        assert '<select class="cell-edit" data-field="visual.framing" ' in html

    def test_free_text_field_renders_input(self, tmp_path):
        work = _copy_fixture(tmp_path)
        html = render_shot_html(work).read_text(encoding="utf-8")
        assert '<input type="text" class="cell-edit" data-field="visual.contentSummary"' in html

    def test_verified_checkbox_has_accessible_label(self, tmp_path):
        work = _copy_fixture(tmp_path)
        html = render_shot_html(work).read_text(encoding="utf-8")
        assert 'class="verify-toggle"' in html
        assert 'aria-label="核实 SH0001 visual.framing"' in html

    def test_unmapped_select_shows_original_and_mapping_hint(self, tmp_path):
        work = _copy_fixture(tmp_path)
        unified = work / "raw" / "unified-media.json"
        data = json.loads(unified.read_text(encoding="utf-8"))
        data["batches"][0]["response"]["shots"][0]["visual"]["framing"] = '"引号" <b>中文</b>'
        unified.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        html = render_shot_html(work).read_text(encoding="utf-8")
        # 原文不得可解析地出现在 option 中；escape 后的原文与“待归类”提示必须可见。
        assert '"引号" <b>中文</b>（待归类）' not in html
        assert "&quot;引号&quot; &lt;b&gt;中文&lt;/b&gt;（待归类）" in html
        # 单元格状态仍为 unmapped。
        assert (
            'data-field="visual.framing" data-shot-id="SH0001" '
            'data-value-state="unmapped"' in html
        )


class TestServerModeAndActions:
    def test_server_mode_injects_review_server_flag(self, tmp_path):
        work = _copy_fixture(tmp_path)
        html = render_shot_html(work, server_mode=True).read_text(encoding="utf-8")
        assert "window.__REVIEW_SERVER__ = true;" in html

    def test_default_offline_mode(self, tmp_path):
        work = _copy_fixture(tmp_path)
        html = render_shot_html(work).read_text(encoding="utf-8")
        assert "window.__REVIEW_SERVER__ = false;" in html

    def test_export_and_confirm_buttons_present(self, tmp_path):
        work = _copy_fixture(tmp_path)
        html = render_shot_html(work).read_text(encoding="utf-8")
        assert 'id="export-corrections"' in html
        assert 'id="confirm-document"' in html
        assert "导出校对记录" in html
        assert "确认本次校对" in html
        # 本地保存按钮存在（server 模式下由 JS 取消 hidden）。
        assert 'id="save-corrections"' in html


class TestReviewReasonsAndBoundary:
    def test_review_reasons_mark_column_header(self, tmp_path):
        # fixture：SH0002 在 shots.json 中 needsReview=False，但 camera-motion
        # (static) 与模型语义（跟）冲突，resolver 产生 review_reasons。
        work = _copy_fixture(tmp_path)
        html = render_shot_html(work).read_text(encoding="utf-8")
        header = next(
            line for line in html.splitlines()
            if 'scope="col" data-shot-id="SH0002"' in line
        )
        assert 'data-needs-review="true"' in header
        assert "title=" in header
        assert "不一致" in header

    @staticmethod
    def _column_reasons(html: str, shot_id: str) -> list[str]:
        """提取镜头列头 data-review-reasons 并解析为字符串数组。"""
        header = next(
            line for line in html.splitlines()
            if f'scope="col" data-shot-id="{shot_id}"' in line
        )
        match = re.search(r'data-review-reasons="([^"]*)"', header)
        assert match, f"{shot_id} 镜头列头必须带 data-review-reasons 机器可读属性"
        reasons = json.loads(html_lib.unescape(match.group(1)))
        assert isinstance(reasons, list)
        assert all(isinstance(r, str) for r in reasons)
        return reasons

    def test_review_reasons_machine_readable_attribute(self, tmp_path):
        # 稳定 HTML 语义：data-review-reasons 为 JSON 字符串数组，
        # SH0002（resolver 冲突）包含冲突理由且 needs-review=true。
        work = _copy_fixture(tmp_path)
        html = render_shot_html(work).read_text(encoding="utf-8")
        reasons = self._column_reasons(html, "SH0002")
        assert any("不一致" in r for r in reasons)

    def test_shots_json_flag_merged_into_reasons(self, tmp_path):
        # SH0001 在 shots.json 中 needsReview=true：标记理由合并进
        # data-review-reasons，与 resolver 理由同一语义通道。
        work = _copy_fixture(tmp_path)
        html = render_shot_html(work).read_text(encoding="utf-8")
        reasons = self._column_reasons(html, "SH0001")
        assert any("needsReview" in r for r in reasons)

    def test_clean_shots_have_empty_reasons_and_no_badge(self, tmp_path):
        # 无冲突且无 needsReview 标记：needs-review=false、reasons=[]、
        # 不渲染空 warning badge（roadmap 03-01 验收：无冲突不显示空容器）。
        work = _copy_fixture(tmp_path)
        shots_path = work / "raw" / "shots.json"
        doc = json.loads(shots_path.read_text(encoding="utf-8"))
        for shot in doc["shots"]:
            shot["needsReview"] = False
        shots_path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        (work / "raw" / "camera-motion.json").unlink()
        html = render_shot_html(work).read_text(encoding="utf-8")
        assert 'data-needs-review="true"' not in html
        assert '<span class="needs-review-badge">' not in html
        assert "需人工复核</span>" not in html
        for shot_id in ("SH0001", "SH0002", "SH0003"):
            assert self._column_reasons(html, shot_id) == []

    def test_needs_review_consistent_with_reasons(self, tmp_path):
        # 不变量：data-needs-review="true" 当且仅当 data-review-reasons 非空。
        work = _copy_fixture(tmp_path)
        html = render_shot_html(work).read_text(encoding="utf-8")
        for shot_id in ("SH0001", "SH0002", "SH0003"):
            header = next(
                line for line in html.splitlines()
                if f'scope="col" data-shot-id="{shot_id}"' in line
            )
            reasons = self._column_reasons(html, shot_id)
            if reasons:
                assert 'data-needs-review="true"' in header
            else:
                assert 'data-needs-review="false"' in header

    def test_boundary_form_moved_to_sidebar_inspector(self, tmp_path):
        work = _copy_fixture(tmp_path)
        html = render_shot_html(work).read_text(encoding="utf-8")
        assert 'class="boundary-form"' not in html
        assert "inspector-boundary" in html
        assert "inspector-boundary-form" in html
        assert "时间范围" in html
        assert 'name = "finalStartMs"' in html
        assert 'name = "finalEndMs"' in html
        assert '"startMs": 0' in html
        assert '"endMs": 3203' in html
        assert "保存时间调整" in html
        # 循环播放开关（默认关）。
        assert 'id="loop-shot"' in html

    def test_validation_summary_rendered_readonly(self, tmp_path):
        work = _copy_fixture(tmp_path)
        html = render_shot_html(work, validation_summary="0 个错误，1 个警告").read_text(
            encoding="utf-8"
        )
        assert 'id="validation-summary"' in html
        assert "0 个错误，1 个警告" in html

    def test_validation_summary_none_renders_placeholder(self, tmp_path):
        work = _copy_fixture(tmp_path)
        html = render_shot_html(work).read_text(encoding="utf-8")
        assert 'id="validation-summary"' in html
        assert "未提供检查结果" in html


class TestAssignMotionLanes:
    def _event(self, left, width, is_point=False):
        return {"left": left, "width": width, "isPoint": is_point}

    def test_empty_events_return_zero_lanes(self):
        assert _assign_motion_lanes([]) == 0

    def test_non_overlapping_events_share_lane_zero(self):
        events = [self._event(0.0, 10.0), self._event(12.0, 10.0)]
        assert _assign_motion_lanes(events) == 1
        assert [e["lane"] for e in events] == [0, 0]

    def test_overlapping_events_spill_to_next_lane(self):
        events = [self._event(0.0, 10.0), self._event(5.0, 10.0)]
        assert _assign_motion_lanes(events) == 2
        assert [e["lane"] for e in events] == [0, 1]

    def test_third_event_reuses_first_lane_when_free(self):
        events = [
            self._event(0.0, 10.0),
            self._event(5.0, 10.0),
            self._event(20.0, 10.0),
        ]
        assert _assign_motion_lanes(events) == 2
        assert [e["lane"] for e in events] == [0, 1, 0]

    def test_point_event_uses_compact_occupancy(self):
        # 点事件按 1.2% 占位：left=2.0 的点事件不占住 left=5.0 之后的泳道。
        events = [self._event(2.0, 2.8, is_point=True), self._event(5.0, 10.0)]
        assert _assign_motion_lanes(events) == 1
        assert [e["lane"] for e in events] == [0, 0]

    def test_gap_prevents_visual_touching(self):
        # 0.4% 间隙：left=10.2 紧跟 right=10.0 但小于 gap，仍分行。
        events = [self._event(0.0, 10.0), self._event(10.2, 5.0)]
        assert _assign_motion_lanes(events) == 2
        assert [e["lane"] for e in events] == [0, 1]

    def test_exact_gap_touch_shares_lane(self):
        # left == 上一事件 right + gap(0.4)：恰好贴满间隙时允许共行。
        events = [self._event(0.0, 10.0), self._event(10.4, 5.0)]
        assert _assign_motion_lanes(events) == 1
        assert [e["lane"] for e in events] == [0, 0]


class TestReviewWorkbenchRendering:
    """Phase 06：审片工作台视图（切点轨道/波形/嵌入 JSON）渲染。"""

    def _render(self, tmp_path: Path) -> Path:
        work = _copy_fixture(tmp_path)
        return render_shot_html(work, status="draft")

    def test_renders_transition_band_and_waveform(self, tmp_path: Path) -> None:
        target = self._render(tmp_path)
        doc = target.read_text(encoding="utf-8")
        assert 'class="transition-band"' in doc
        assert 'class="transition-marker' in doc
        # 夹具无音轨 → 波形轨道存在但显式 unavailable（不伪造空波形）
        assert 'class="waveform-band"' in doc
        assert "波形不可用" in doc
        assert "REVIEW_WORKBENCH" in doc
        assert _errors(target, target.parent) == []

    def test_waveform_canvas_when_available(self, tmp_path: Path) -> None:
        work = _copy_fixture(tmp_path)
        rt = json.loads(
            (work / "raw" / "review-timeline.json").read_text(encoding="utf-8")
        )
        rt["waveform"] = {
            "status": "complete",
            "channelMode": "mono-mixdown",
            "binDurationMs": 20,
            "binCount": 4,
            "peaks": [[-0.4, 0.4], [-0.2, 0.3], [0.0, 0.1], [-0.1, 0.2]],
        }
        (work / "raw" / "review-timeline.json").write_text(
            json.dumps(rt, ensure_ascii=False), encoding="utf-8"
        )
        target = render_shot_html(work, status="draft")
        doc = target.read_text(encoding="utf-8")
        assert 'id="waveform-canvas"' in doc
        assert _errors(target, target.parent) == []

    def test_workbench_json_carries_pairs_and_frame_source(self, tmp_path: Path) -> None:
        target = self._render(tmp_path)
        doc = target.read_text(encoding="utf-8")
        match = re.search(
            r"var REVIEW_WORKBENCH = (.*?);\n", doc, re.DOTALL
        )
        assert match, "REVIEW_WORKBENCH 未嵌入"
        payload = json.loads(match.group(1))
        assert payload["frameSource"]["mode"] in ("pts", "approx")
        assert len(payload["transitions"]) == 2
        first = payload["transitions"][0]
        assert first["pairID"] == "SH0001--SH0002"
        assert "lumaDelta" in first["metrics"]
        assert first["semanticStatus"] == "unknown"

    def test_frame_refs_are_html_escaped(self, tmp_path: Path) -> None:
        target = self._render(tmp_path)
        doc = target.read_text(encoding="utf-8")
        match = re.search(r"var REVIEW_WORKBENCH = (.*?);\n", doc, re.DOTALL)
        assert match
        # 嵌入 JSON 已转义 & < >（_json_for_script 契约），不含 script 闭合
        assert "</script>" not in match.group(1)
