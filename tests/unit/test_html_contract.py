"""validate.html_contract 单元测试：手工构造非法 HTML，逐项验证捕获能力。

契约依据：docs/04 §8.1 结构、§8.2 单元格（含编辑控件放行）、§8.3 安全、
§8.4 严格数据一致性（含 corrections 状态核对）。
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from memoloupe.validate.html_contract import HTML_CONTRACT_VERSION, validate_html

VALID_DOC = """<!DOCTYPE html>
<html data-document-type="shotAnalysis" data-document-status="draft"
      data-contract-version="1.0" data-source-revision="a1b2c3d4e5f6">
<head><meta charset="utf-8"><title>shot</title></head>
<body>
<button type="button" id="confirm-document">确认文档</button>
<table id="shot-table">
  <thead>
    <tr>
      <th scope="col">字段</th>
      <th scope="col" data-shot-id="SH0001" data-start-ms="0" data-end-ms="3203" data-needs-review="false" data-review-reasons="[]">SH0001</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <th scope="row">visual.framing</th>
      <td data-field="visual.framing" data-shot-id="SH0001" data-value-state="value"
          data-confidence="medium" data-source="unifiedModel" data-verified="false"
          data-evidence-refs="raw/unified-media.json#batches[0].response.shots[0].visual.framing">全景</td>
    </tr>
  </tbody>
</table>
</body>
</html>
"""


def _validate(tmp_path: Path, html: str, **kwargs):
    path = tmp_path / "shot-analysis.html"
    path.write_text(html, encoding="utf-8")
    return validate_html(path, **kwargs)


def _errors(issues):
    return [i for i in issues if i.severity == "error"]


def _messages(issues) -> str:
    return "\n".join(i.message for i in issues)


def _write_html_ref_targets(root: Path) -> None:
    raw = root / "raw"
    raw.mkdir(exist_ok=True)
    (raw / "unified-media.json").write_text(
        json.dumps(
            {
                "batches": [
                    {
                        "response": {
                            "shots": [
                                {"visual": {"framing": "全景"}},
                            ]
                        }
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (raw / "audio-energy.json").write_text(
        json.dumps({"shots": [{}, {}]}, ensure_ascii=False),
        encoding="utf-8",
    )


class TestValidDocument:
    def test_valid_doc_has_no_error(self, tmp_path):
        assert _errors(_validate(tmp_path, VALID_DOC)) == []

    def test_artifact_name_is_file_name(self, tmp_path):
        issues = _validate(tmp_path, VALID_DOC.replace("shotAnalysis", "bogus"))
        assert issues and all(i.artifact == "shot-analysis.html" for i in issues)

    def test_label_only_cell_needs_no_five_state_attrs(self, tmp_path):
        doc = VALID_DOC.replace(
            '<td data-field="visual.framing" data-shot-id="SH0001" data-value-state="value"\n'
            '          data-confidence="medium" data-source="unifiedModel" data-verified="false"\n'
            '          data-evidence-refs="raw/unified-media.json#batches[0].response.shots[0].visual.framing">全景</td>',
            '<td data-field="visual.framing" data-shot-id="SH0001" data-value-state="value"'
            ' data-confidence="medium" data-source="unifiedModel" data-verified="false"'
            ' data-evidence-refs="raw/shots.json#shots[0]">全景</td></tr><tr>'
            '<th scope="row">说明</th>'
            '<td data-field="note" data-value-state="labelOnly">仅供参考</td>',
        )
        assert _errors(_validate(tmp_path, doc)) == []

    def test_issues_carry_line_numbers(self, tmp_path):
        doc = VALID_DOC.replace('data-document-status="draft"', "")
        issues = _errors(_validate(tmp_path, doc))
        assert issues and all(i.json_path.startswith("L") for i in issues)

    def test_contract_version_constant(self):
        assert HTML_CONTRACT_VERSION == "htmlcheck.v1"


class TestStructure:
    def test_wrong_document_type(self, tmp_path):
        issues = _errors(_validate(tmp_path, VALID_DOC.replace("shotAnalysis", "shot")))
        assert "data-document-type" in _messages(issues)

    def test_missing_document_status(self, tmp_path):
        issues = _errors(_validate(tmp_path, VALID_DOC.replace('data-document-status="draft"\n      ', "")))
        assert "data-document-status" in _messages(issues)

    def test_invalid_document_status(self, tmp_path):
        issues = _errors(_validate(tmp_path, VALID_DOC.replace('data-document-status="draft"', 'data-document-status="final"')))
        assert "data-document-status" in _messages(issues)

    def test_missing_contract_version(self, tmp_path):
        issues = _errors(_validate(tmp_path, VALID_DOC.replace('data-contract-version="1.0" ', "")))
        assert "data-contract-version" in _messages(issues)

    def test_missing_source_revision(self, tmp_path):
        issues = _errors(_validate(tmp_path, VALID_DOC.replace('data-source-revision="a1b2c3d4e5f6"', "")))
        assert "data-source-revision" in _messages(issues)

    def test_duplicate_id(self, tmp_path):
        doc = VALID_DOC.replace("</table>", '</table><table id="shot-table"><tbody><tr><td>x</td></tr></tbody></table>')
        issues = _errors(_validate(tmp_path, doc))
        assert "id" in _messages(issues) and "重复" in _messages(issues)

    def test_tr_directly_under_table_reports_missing_tbody(self, tmp_path):
        doc = VALID_DOC.replace("  <tbody>\n", "").replace("  </tbody>\n", "")
        issues = _errors(_validate(tmp_path, doc))
        assert "tbody" in _messages(issues)
        # 不得只报模糊 parse error：必须点名 tbody。
        assert any("tbody" in i.message for i in issues)

    def test_table_without_any_tbody(self, tmp_path):
        doc = VALID_DOC.replace("<tbody>", "").replace("</tbody>", "").replace("<thead>", "").replace("</thead>", "")
        issues = _errors(_validate(tmp_path, doc))
        assert any("tbody" in i.message for i in issues)

    def test_story_block_forbidden_in_shot_analysis(self, tmp_path):
        doc = VALID_DOC.replace(
            "</body>",
            '<section class="story-block" data-story-block-id="B0001"></section>\n</body>',
        )
        issues = _errors(_validate(tmp_path, doc))
        assert "story-block" in _messages(issues)

    def test_story_block_allowed_in_story_analysis(self, tmp_path):
        doc = VALID_DOC.replace("shotAnalysis", "storyAnalysis").replace(
            "</body>",
            '<section class="story-block" data-story-block-id="B0001" '
            'data-shot-ids="SH0001" data-start-ms="0" data-end-ms="3203">'
            '<table><tbody><tr>'
            '<td data-field="primaryRole" data-block-id="B0001" '
            'data-value-state="value" data-confidence="high" '
            'data-source="textModel" data-verified="false" '
            'data-evidence-refs="raw/story-blocks.json#blocks[0].primaryRole">hook</td>'
            "</tr></tbody></table></section>\n</body>",
        )
        assert _errors(_validate(tmp_path, doc)) == []

    def test_shot_analysis_requires_shot_column(self, tmp_path):
        doc = VALID_DOC.replace(
            '<th scope="col" data-shot-id="SH0001" data-start-ms="0" data-end-ms="3203" data-needs-review="false" data-review-reasons="[]">SH0001</th>',
            "<th scope=\"col\">SH0001</th>",
        )
        issues = _errors(_validate(tmp_path, doc))
        assert "镜头列" in _messages(issues)


class TestReviewReasonsSemantics:
    """data-review-reasons 机器可读语义（roadmap 03-01）：

    镜头列头必须携带 data-needs-review ∈ {true,false} 与
    data-review-reasons（JSON 字符串数组），且二者一致：
    needs-review="true" 当且仅当 reasons 非空。
    """

    COL = (
        '<th scope="col" data-shot-id="SH0001" data-start-ms="0" data-end-ms="3203"'
        ' data-needs-review="false" data-review-reasons="[]">SH0001</th>'
    )

    def _variant(self, tmp_path, old: str, new: str):
        return _errors(_validate(tmp_path, VALID_DOC.replace(old, new)))

    def test_valid_true_with_reasons_passes(self, tmp_path):
        doc = VALID_DOC.replace(
            'data-needs-review="false" data-review-reasons="[]"',
            'data-needs-review="true" data-review-reasons="[&quot;运镜冲突&quot;]"',
        )
        assert _errors(_validate(tmp_path, doc)) == []

    def test_missing_needs_review_attribute(self, tmp_path):
        issues = self._variant(tmp_path, ' data-needs-review="false"', "")
        assert "data-needs-review" in _messages(issues)

    def test_invalid_needs_review_value(self, tmp_path):
        issues = self._variant(
            tmp_path, 'data-needs-review="false"', 'data-needs-review="maybe"'
        )
        assert "data-needs-review" in _messages(issues)

    def test_missing_review_reasons_attribute(self, tmp_path):
        issues = self._variant(tmp_path, ' data-review-reasons="[]"', "")
        assert "data-review-reasons" in _messages(issues)

    def test_reasons_must_be_json_array(self, tmp_path):
        issues = self._variant(
            tmp_path, 'data-review-reasons="[]"', 'data-review-reasons="not-json"'
        )
        assert "data-review-reasons" in _messages(issues)

    def test_reasons_must_be_string_array(self, tmp_path):
        issues = self._variant(
            tmp_path, 'data-review-reasons="[]"', 'data-review-reasons="[1]"'
        )
        assert "data-review-reasons" in _messages(issues)

    def test_true_with_empty_reasons_is_inconsistent(self, tmp_path):
        issues = self._variant(
            tmp_path, 'data-needs-review="false"', 'data-needs-review="true"'
        )
        assert any(
            "data-review-reasons" in i.message and "data-needs-review" in i.message
            for i in issues
        )

    def test_false_with_nonempty_reasons_is_inconsistent(self, tmp_path):
        issues = self._variant(
            tmp_path, 'data-review-reasons="[]"', 'data-review-reasons="[&quot;x&quot;]"'
        )
        assert any(
            "data-review-reasons" in i.message and "data-needs-review" in i.message
            for i in issues
        )


class TestStrictReviewReasonsCrossCheck:
    """strict 模式：raw/shots.json 的 needsReview 标记与页面列头交叉核对。"""

    def _make_root(self, tmp_path: Path, *, needs_review: bool) -> Path:
        raw = tmp_path / "raw"
        raw.mkdir()
        (raw / "shots.json").write_text(
            json.dumps({"shots": [
                {"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 3203,
                 "needsReview": needs_review},
            ]}),
            encoding="utf-8",
        )
        (raw / "media.json").write_text(
            json.dumps({"source": {"revisionID": "a1b2c3d4e5f6"}}), encoding="utf-8"
        )
        _write_html_ref_targets(tmp_path)
        return tmp_path

    def test_raw_flag_true_requires_page_true(self, tmp_path):
        root = self._make_root(tmp_path, needs_review=True)
        issues = _errors(_validate(tmp_path, VALID_DOC, root=root, strict=True))
        assert "needsReview" in _messages(issues)

    def test_raw_flag_true_with_page_true_passes(self, tmp_path):
        root = self._make_root(tmp_path, needs_review=True)
        doc = VALID_DOC.replace(
            'data-needs-review="false" data-review-reasons="[]"',
            'data-needs-review="true" '
            'data-review-reasons="[&quot;shots.json 标记 needsReview&quot;]"',
        )
        assert _errors(_validate(tmp_path, doc, root=root, strict=True)) == []

    def test_page_true_without_raw_flag_passes(self, tmp_path):
        # resolver 冲突理由可独立置 true，不要求 raw 有标记。
        root = self._make_root(tmp_path, needs_review=False)
        doc = VALID_DOC.replace(
            'data-needs-review="false" data-review-reasons="[]"',
            'data-needs-review="true" data-review-reasons="[&quot;运镜冲突&quot;]"',
        )
        assert _errors(_validate(tmp_path, doc, root=root, strict=True)) == []


class TestCells:
    def _cell_variant(self, tmp_path, old: str, new: str):
        return _errors(_validate(tmp_path, VALID_DOC.replace(old, new)))

    def test_invalid_value_state(self, tmp_path):
        issues = self._cell_variant(tmp_path, 'data-value-state="value"', 'data-value-state="maybe"')
        assert "data-value-state" in _messages(issues)

    def test_missing_value_state(self, tmp_path):
        issues = self._cell_variant(tmp_path, ' data-value-state="value"', "")
        assert "data-value-state" in _messages(issues)

    def test_missing_confidence(self, tmp_path):
        issues = self._cell_variant(tmp_path, ' data-confidence="medium"', "")
        assert "data-confidence" in _messages(issues)

    def test_missing_source(self, tmp_path):
        issues = self._cell_variant(tmp_path, ' data-source="unifiedModel"', "")
        assert "data-source" in _messages(issues)

    def test_missing_verified(self, tmp_path):
        issues = self._cell_variant(tmp_path, ' data-verified="false"', "")
        assert "data-verified" in _messages(issues)

    def test_verified_must_be_true_or_false(self, tmp_path):
        issues = self._cell_variant(tmp_path, 'data-verified="false"', 'data-verified="maybe"')
        assert "data-verified" in _messages(issues)

    def test_bad_evidence_ref(self, tmp_path):
        issues = self._cell_variant(
            tmp_path,
            'data-evidence-refs="raw/unified-media.json#batches[0].response.shots[0].visual.framing"',
            'data-evidence-refs="../secret.json#shots[0]"',
        )
        assert "data-evidence-refs" in _messages(issues)

    def test_multiple_refs_all_validated(self, tmp_path):
        issues = self._cell_variant(
            tmp_path,
            'data-evidence-refs="raw/unified-media.json#batches[0].response.shots[0].visual.framing"',
            'data-evidence-refs="raw/shots.json#shots[0] raw/bad.json#"',
        )
        assert "data-evidence-refs" in _messages(issues)

    def test_unknown_cell_may_have_empty_refs(self, tmp_path):
        # 保留原有带证据的单元格，另加一行 unknown 且 refs 为空的单元格。
        doc = VALID_DOC.replace(
            "</tr>\n  </tbody>",
            '</tr><tr><th scope="row">visual.contentSummary</th>'
            '<td data-field="visual.contentSummary" data-shot-id="SH0001" data-value-state="unknown"'
            ' data-confidence="unknown" data-source="fallback" data-verified="false"'
            ' data-evidence-refs="">未知</td></tr>\n  </tbody>',
        )
        assert _errors(_validate(tmp_path, doc)) == []


class TestSecurity:
    def test_external_script_src(self, tmp_path):
        doc = VALID_DOC.replace("</body>", '<script src="app.js"></script>\n</body>')
        assert "script" in _messages(_errors(_validate(tmp_path, doc)))

    def test_cdn_script_src(self, tmp_path):
        doc = VALID_DOC.replace("</body>", '<script src="https://cdn.example.com/app.js"></script>\n</body>')
        assert _errors(_validate(tmp_path, doc))

    def test_javascript_url(self, tmp_path):
        doc = VALID_DOC.replace("</body>", '<a href="javascript:void(0)">x</a>\n</body>')
        assert "javascript:" in _messages(_errors(_validate(tmp_path, doc)))

    def test_http_image(self, tmp_path):
        doc = VALID_DOC.replace("</body>", '<img src="https://cdn.example.com/x.jpg">\n</body>')
        assert _errors(_validate(tmp_path, doc))

    def test_inline_script_allowed(self, tmp_path):
        doc = VALID_DOC.replace("</body>", "<script>console.log(1)</script>\n</body>")
        assert _errors(_validate(tmp_path, doc)) == []


class TestStrictConsistency:
    def _make_root(self, tmp_path: Path) -> Path:
        raw = tmp_path / "raw"
        raw.mkdir()
        (raw / "shots.json").write_text(
            json.dumps({"shots": [
                {"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 3203},
                {"shotID": "SH0002", "finalStartMs": 3203, "finalEndMs": 6400},
            ]}),
            encoding="utf-8",
        )
        (raw / "media.json").write_text(
            json.dumps({"source": {"revisionID": "a1b2c3d4e5f6"}}), encoding="utf-8"
        )
        _write_html_ref_targets(tmp_path)
        return tmp_path

    def test_strict_consistent_doc_passes(self, tmp_path):
        root = self._make_root(tmp_path)
        # 页面只展示 SH0001 不行——必须与 shots.json 集合一致。
        doc = VALID_DOC.replace(
            '<th scope="col" data-shot-id="SH0001" data-start-ms="0" data-end-ms="3203" data-needs-review="false" data-review-reasons="[]">SH0001</th>',
            '<th scope="col" data-shot-id="SH0001" data-start-ms="0" data-end-ms="3203" data-needs-review="false" data-review-reasons="[]">SH0001</th>'
            '<th scope="col" data-shot-id="SH0002" data-start-ms="3203" data-end-ms="6400" data-needs-review="false" data-review-reasons="[]">SH0002</th>',
        )
        issues = _errors(_validate(tmp_path, doc, root=root, strict=True))
        # SH0002 列缺少可追溯证据单元格 -> 仍报错；补上单元格后应为零。
        doc = doc.replace(
            "</tr>\n  </tbody>",
            '<td data-field="audio.energy" data-shot-id="SH0002" data-value-state="value"'
            ' data-confidence="high" data-source="ffmpeg" data-verified="false"'
            ' data-evidence-refs="raw/audio-energy.json#shots[1]">低</td></tr>\n  </tbody>',
        ).replace(
            '<th scope="row">visual.framing</th>', '<th scope="row">audio.energy</th>',
        )
        assert _errors(_validate(tmp_path, doc, root=root, strict=True)) == []

    def test_strict_shot_set_mismatch(self, tmp_path):
        root = self._make_root(tmp_path)
        issues = _errors(_validate(tmp_path, VALID_DOC, root=root, strict=True))
        assert "shotID" in _messages(issues) or "镜头" in _messages(issues)

    def test_strict_time_boundary_mismatch(self, tmp_path):
        root = self._make_root(tmp_path)
        doc = VALID_DOC.replace(
            '<th scope="col" data-shot-id="SH0001" data-start-ms="0" data-end-ms="3203" data-needs-review="false" data-review-reasons="[]">SH0001</th>',
            '<th scope="col" data-shot-id="SH0001" data-start-ms="0" data-end-ms="3203" data-needs-review="false" data-review-reasons="[]">SH0001</th>'
            '<th scope="col" data-shot-id="SH0002" data-start-ms="3203" data-end-ms="9999" data-needs-review="false" data-review-reasons="[]">SH0002</th>',
        )
        issues = _errors(_validate(tmp_path, doc, root=root, strict=True))
        assert "finalEndMs" in _messages(issues) or "data-end-ms" in _messages(issues)

    def test_strict_revision_mismatch(self, tmp_path):
        root = self._make_root(tmp_path)
        doc = VALID_DOC.replace('data-source-revision="a1b2c3d4e5f6"', 'data-source-revision="deadbeef"')
        issues = _errors(_validate(tmp_path, doc, root=root, strict=True))
        assert "revision" in _messages(issues)

    def test_strict_missing_shots_json(self, tmp_path):
        (tmp_path / "raw").mkdir()
        issues = _errors(_validate(tmp_path, VALID_DOC, root=tmp_path, strict=True))
        assert "shots.json" in _messages(issues)

    def test_non_strict_skips_consistency(self, tmp_path):
        root = self._make_root(tmp_path)
        doc = VALID_DOC.replace('data-source-revision="a1b2c3d4e5f6"', 'data-source-revision="deadbeef"')
        assert _errors(_validate(tmp_path, doc, root=root, strict=False)) == []


class TestEditingControlsAllowed:
    """M3：单元格内的内联编辑控件不触发误报（docs/04 §5/§8.2）。"""

    def test_select_input_textarea_controls_allowed(self, tmp_path):
        doc = VALID_DOC.replace(
            ">全景</td>",
            '><span class="cell-text">全景</span>'
            '<select class="cell-edit" data-field="visual.framing" data-shot-id="SH0001"'
            ' aria-label="编辑 SH0001 visual.framing">'
            '<option value="远景">远景</option>'
            '<option value="全景" selected>全景</option>'
            '<option value="unknown">unknown</option>'
            "</select>"
            '<input type="text" class="cell-edit" value="全景" aria-label="编辑备注">'
            '<textarea aria-label="备注"></textarea>'
            '<label><input type="checkbox" class="verify-toggle"'
            ' aria-label="核实 SH0001 visual.framing"> 已核实</label></td>',
        )
        assert _errors(_validate(tmp_path, doc)) == []

    def test_original_value_and_verified_true_allowed(self, tmp_path):
        doc = VALID_DOC.replace(
            'data-source="unifiedModel" data-verified="false"',
            'data-source="human" data-verified="true" data-original-value="全景"',
        )
        assert _errors(_validate(tmp_path, doc)) == []


class TestStoryAnalysisDocument:
    """storyAnalysis 文档结构（docs/04 §4、roadmap 03-04）。

    story-block DOM 只允许出现在 storyAnalysis；storyAnalysis 必须包含
    至少一个 story-block，且块头携带 data-story-block-id/data-shot-ids/
    data-start-ms/data-end-ms；每 block 至少一个可追溯证据单元格。
    """

    STORY_DOC = """<!DOCTYPE html>
<html data-document-type="storyAnalysis" data-document-status="draft"
      data-contract-version="1.0" data-source-revision="a1b2c3d4e5f6">
<head><meta charset="utf-8"><title>story</title></head>
<body>
<button type="button" id="confirm-document">确认文档</button>
<section class="story-block" data-story-block-id="B0001" data-shot-ids="SH0001"
         data-start-ms="0" data-end-ms="3203">
  <table><tbody><tr>
    <td data-field="primaryRole" data-block-id="B0001" data-value-state="value"
        data-confidence="high" data-source="textModel" data-verified="false"
        data-evidence-refs="raw/story-blocks.json#blocks[0].primaryRole">hook</td>
  </tr></tbody></table>
</section>
</body>
</html>
"""

    def test_valid_story_doc_has_no_error(self, tmp_path):
        issues = _errors(_validate(tmp_path, self.STORY_DOC))
        assert issues == []

    def test_story_analysis_requires_story_block(self, tmp_path):
        doc = self.STORY_DOC.replace(
            '<section class="story-block" data-story-block-id="B0001" data-shot-ids="SH0001"\n'
            '         data-start-ms="0" data-end-ms="3203">',
            "<section>",
        )
        issues = _errors(_validate(tmp_path, doc))
        assert "story-block" in _messages(issues)

    def test_story_block_missing_required_attrs(self, tmp_path):
        doc = self.STORY_DOC.replace(' data-shot-ids="SH0001"', "")
        issues = _errors(_validate(tmp_path, doc))
        assert "data-shot-ids" in _messages(issues)

    def test_story_block_missing_start_end(self, tmp_path):
        doc = self.STORY_DOC.replace(" data-start-ms=\"0\" data-end-ms=\"3203\"", "")
        issues = _errors(_validate(tmp_path, doc))
        assert "data-start-ms" in _messages(issues)
        assert "data-end-ms" in _messages(issues)

    def test_story_block_requires_traceable_cell(self, tmp_path):
        doc = self.STORY_DOC.replace(
            ' data-evidence-refs="raw/story-blocks.json#blocks[0].primaryRole"', ""
        )
        issues = _errors(_validate(tmp_path, doc))
        assert "data-evidence-refs" in _messages(issues)

    def test_story_block_cell_without_block_id_is_ignored_for_coverage(self, tmp_path):
        doc = self.STORY_DOC.replace(" data-block-id=\"B0001\"", "")
        issues = _errors(_validate(tmp_path, doc))
        assert "data-evidence-refs" in _messages(issues)

    def test_empty_shot_ids_rejected(self, tmp_path):
        doc = self.STORY_DOC.replace('data-shot-ids="SH0001"', 'data-shot-ids=""')
        issues = _errors(_validate(tmp_path, doc))
        assert "data-shot-ids" in _messages(issues)


class TestStoryStrictConsistency:
    """strict：storyAnalysis 页面 story-block 与 raw/story-blocks.json 对齐。"""

    STORY_BLOCKS = {
        "status": "complete",
        "boundarySource": "asr-gap",
        "gapMs": 1200,
        "generatedAt": "2026-08-25T00:00:00Z",
        "blocks": [
            {"storyBlockID": "B0001", "shotIDs": ["SH0001"],
             "startMs": 0, "endMs": 3203, "primaryRole": "hook"},
        ],
        "slots": [],
    }

    def _make_root(self, tmp_path: Path) -> Path:
        raw = tmp_path / "raw"
        raw.mkdir()
        (raw / "shots.json").write_text(
            json.dumps({"shots": [
                {"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 3203},
            ]}),
            encoding="utf-8",
        )
        (raw / "media.json").write_text(
            json.dumps({"source": {"revisionID": "a1b2c3d4e5f6"}}), encoding="utf-8"
        )
        (raw / "story-blocks.json").write_text(
            json.dumps(self.STORY_BLOCKS, ensure_ascii=False), encoding="utf-8"
        )
        return tmp_path

    def test_consistent_story_doc_passes_strict(self, tmp_path):
        root = self._make_root(tmp_path)
        assert _errors(_validate(tmp_path, TestStoryAnalysisDocument.STORY_DOC,
                                 root=root, strict=True)) == []

    def test_block_id_set_mismatch(self, tmp_path):
        root = self._make_root(tmp_path)
        doc = TestStoryAnalysisDocument.STORY_DOC.replace(
            'data-story-block-id="B0001"', 'data-story-block-id="B9999"'
        )
        issues = _errors(_validate(tmp_path, doc, root=root, strict=True))
        assert "story-block ID 集合" in _messages(issues)

    def test_block_shot_ids_mismatch(self, tmp_path):
        root = self._make_root(tmp_path)
        doc = TestStoryAnalysisDocument.STORY_DOC.replace(
            'data-shot-ids="SH0001"', 'data-shot-ids="SH0001 SH0002"'
        )
        issues = _errors(_validate(tmp_path, doc, root=root, strict=True))
        assert "data-shot-ids" in _messages(issues)

    def test_block_time_mismatch(self, tmp_path):
        root = self._make_root(tmp_path)
        doc = TestStoryAnalysisDocument.STORY_DOC.replace(
            'data-end-ms="3203"', 'data-end-ms="9999"'
        )
        issues = _errors(_validate(tmp_path, doc, root=root, strict=True))
        assert "data-end-ms" in _messages(issues)

    def test_block_unknown_shot_reference(self, tmp_path):
        root = self._make_root(tmp_path)
        doc = TestStoryAnalysisDocument.STORY_DOC.replace(
            'data-shot-ids="SH0001"', 'data-shot-ids="SH0001 SH9999"'
        )
        issues = _errors(_validate(tmp_path, doc, root=root, strict=True))
        assert "SH9999" in _messages(issues)

    def test_html_evidence_ref_pointer_must_resolve_strict(self, tmp_path):
        root = self._make_root(tmp_path)
        doc = TestStoryAnalysisDocument.STORY_DOC.replace(
            "raw/story-blocks.json#blocks[0].primaryRole",
            "raw/story-blocks.json#blocks[0].slotType",
        )
        issues = _errors(_validate(tmp_path, doc, root=root, strict=True))
        assert "data-evidence-refs 指针不可解析" in _messages(issues)

    def test_missing_story_blocks_json(self, tmp_path):
        root = self._make_root(tmp_path)
        (root / "raw" / "story-blocks.json").unlink()
        issues = _errors(_validate(tmp_path, TestStoryAnalysisDocument.STORY_DOC,
                                   root=root, strict=True))
        assert "story-blocks.json" in _messages(issues)

    def test_non_strict_skips_story_consistency(self, tmp_path):
        root = self._make_root(tmp_path)
        doc = TestStoryAnalysisDocument.STORY_DOC.replace(
            'data-source-revision="a1b2c3d4e5f6"', 'data-source-revision="deadbeef"'
        )
        assert _errors(_validate(tmp_path, doc, root=root, strict=False)) == []


class TestConfirmButton:
    """页面必须有带可访问名称的确认按钮（docs/04 §2：确认必须显式）。"""

    def test_missing_confirm_button(self, tmp_path):
        doc = VALID_DOC.replace(
            '<button type="button" id="confirm-document">确认文档</button>\n', ""
        )
        issues = _errors(_validate(tmp_path, doc))
        assert "确认" in _messages(issues)

    def test_confirm_button_without_accessible_name(self, tmp_path):
        doc = VALID_DOC.replace(
            '<button type="button" id="confirm-document">确认文档</button>',
            '<button type="button" id="confirm-document"></button>',
        )
        issues = _errors(_validate(tmp_path, doc))
        assert "可访问名称" in _messages(issues)

    def test_confirm_button_with_aria_label_only(self, tmp_path):
        doc = VALID_DOC.replace(
            '<button type="button" id="confirm-document">确认文档</button>',
            '<button type="button" id="confirm-document" aria-label="确认文档"></button>',
        )
        assert _errors(_validate(tmp_path, doc)) == []


class TestStrictCorrectionsStatus:
    """strict：data-document-status 与 corrections 文件状态一致（docs/04 §8.4）。"""

    def _make_root(self, tmp_path: Path, *, with_corrections: bool) -> Path:
        raw = tmp_path / "raw"
        raw.mkdir()
        (raw / "shots.json").write_text(
            json.dumps({"shots": [
                {"shotID": "SH0001", "finalStartMs": 0, "finalEndMs": 3203},
            ]}),
            encoding="utf-8",
        )
        (raw / "media.json").write_text(
            json.dumps({"source": {"revisionID": "a1b2c3d4e5f6"}}), encoding="utf-8"
        )
        _write_html_ref_targets(tmp_path)
        if with_corrections:
            corr_dir = tmp_path / "corrections"
            corr_dir.mkdir()
            (corr_dir / "shotAnalysis.json").write_text(
                json.dumps({
                    "correctionVersion": 1,
                    "documentType": "shotAnalysis",
                    "sourceRevisionID": "a1b2c3d4e5f6",
                    "changes": [],
                }),
                encoding="utf-8",
            )
        return tmp_path

    def _install_fake_corrections(self, monkeypatch, status: str):
        mod = types.ModuleType("memoloupe.render.corrections")
        mod.load_corrections = lambda out_dir, document_type: {"documentType": document_type}
        mod.document_status = lambda corrections, current_revision: status
        monkeypatch.setitem(sys.modules, "memoloupe.render.corrections", mod)

    def test_no_corrections_file_requires_draft(self, tmp_path):
        root = self._make_root(tmp_path, with_corrections=False)
        doc = VALID_DOC.replace('data-document-status="draft"', 'data-document-status="underReview"')
        issues = _errors(_validate(tmp_path, doc, root=root, strict=True))
        assert "draft" in _messages(issues) and "corrections" in _messages(issues)

    def test_no_corrections_file_draft_passes(self, tmp_path):
        root = self._make_root(tmp_path, with_corrections=False)
        assert _errors(_validate(tmp_path, VALID_DOC, root=root, strict=True)) == []

    def test_status_mismatch_with_corrections(self, tmp_path, monkeypatch):
        root = self._make_root(tmp_path, with_corrections=True)
        self._install_fake_corrections(monkeypatch, "underReview")
        issues = _errors(_validate(tmp_path, VALID_DOC, root=root, strict=True))
        status_issues = [i for i in issues if "data-document-status" in i.message]
        assert status_issues
        assert status_issues[0].expected == "underReview"
        assert status_issues[0].actual == "draft"

    def test_status_consistent_with_corrections(self, tmp_path, monkeypatch):
        root = self._make_root(tmp_path, with_corrections=True)
        self._install_fake_corrections(monkeypatch, "underReview")
        doc = VALID_DOC.replace('data-document-status="draft"', 'data-document-status="underReview"')
        assert _errors(_validate(tmp_path, doc, root=root, strict=True)) == []

    def test_corrections_module_unavailable_warns_not_errors(self, tmp_path, monkeypatch):
        root = self._make_root(tmp_path, with_corrections=True)
        # sys.modules 置 None 使 import 抛 ImportError，模拟并行模块尚不存在。
        monkeypatch.setitem(sys.modules, "memoloupe.render.corrections", None)
        issues = _validate(tmp_path, VALID_DOC, root=root, strict=True)
        assert _errors(issues) == []
        assert any(i.severity == "warning" and "corrections" in i.message for i in issues)
