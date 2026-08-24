"""validate.html_contract 单元测试：手工构造非法 HTML，逐项验证捕获能力。

契约依据：docs/04 §8.1 结构、§8.2 单元格、§8.3 安全、§8.4 严格数据一致性。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memoloupe.validate.html_contract import HTML_CONTRACT_VERSION, validate_html

VALID_DOC = """<!DOCTYPE html>
<html data-document-type="shotAnalysis" data-document-status="draft"
      data-contract-version="1.0" data-source-revision="a1b2c3d4e5f6">
<head><meta charset="utf-8"><title>shot</title></head>
<body>
<table id="shot-table">
  <thead>
    <tr>
      <th scope="col">字段</th>
      <th scope="col" data-shot-id="SH0001" data-start-ms="0" data-end-ms="3203">SH0001</th>
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
            '<section class="story-block" data-story-block-id="B0001"></section>\n</body>',
        )
        assert _errors(_validate(tmp_path, doc)) == []

    def test_shot_analysis_requires_shot_column(self, tmp_path):
        doc = VALID_DOC.replace(
            '<th scope="col" data-shot-id="SH0001" data-start-ms="0" data-end-ms="3203">SH0001</th>',
            "<th scope=\"col\">SH0001</th>",
        )
        issues = _errors(_validate(tmp_path, doc))
        assert "镜头列" in _messages(issues)


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
            '</tr><tr><th scope="row">visual.content</th>'
            '<td data-field="visual.content" data-shot-id="SH0001" data-value-state="unknown"'
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
        return tmp_path

    def test_strict_consistent_doc_passes(self, tmp_path):
        root = self._make_root(tmp_path)
        # 页面只展示 SH0001 不行——必须与 shots.json 集合一致。
        doc = VALID_DOC.replace(
            '<th scope="col" data-shot-id="SH0001" data-start-ms="0" data-end-ms="3203">SH0001</th>',
            '<th scope="col" data-shot-id="SH0001" data-start-ms="0" data-end-ms="3203">SH0001</th>'
            '<th scope="col" data-shot-id="SH0002" data-start-ms="3203" data-end-ms="6400">SH0002</th>',
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
            '<th scope="col" data-shot-id="SH0001" data-start-ms="0" data-end-ms="3203">SH0001</th>',
            '<th scope="col" data-shot-id="SH0001" data-start-ms="0" data-end-ms="3203">SH0001</th>'
            '<th scope="col" data-shot-id="SH0002" data-start-ms="3203" data-end-ms="9999">SH0002</th>',
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
