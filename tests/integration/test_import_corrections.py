"""``memoloupe import-corrections`` 集成测试（docs/04 §5.1 离线导出模式）。

四条错误路径（文件缺失 / JSON 非法 / schema 不过 / revision 不匹配）退出码 3；
合法导入退出码 0，修正落盘并重渲染；confirmedAt/confirmedBy 合并幂等。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from memoloupe.cli.main import EXIT_INPUT, EXIT_OK, main
from memoloupe.render.corrections import load_corrections
from memoloupe.render.shot_html import render_shot_html

FIXTURE_FULL = Path(__file__).parent.parent / "fixtures" / "output_full"
FIXTURE_REVISION = "a1b2c3d4e5f6"


@pytest.fixture
def out_dir(tmp_path) -> Path:
    work = tmp_path / "out"
    shutil.copytree(FIXTURE_FULL, work)
    render_shot_html(work)
    return work


def _export_file(tmp_path: Path, **overrides) -> Path:
    doc = {
        "correctionVersion": 1,
        "documentType": "shotAnalysis",
        "sourceRevisionID": FIXTURE_REVISION,
        "changes": [
            {
                "entityID": "SH0001",
                "field": "visual.framing",
                "oldValue": "全景",
                "newValue": "特写",
                "state": "value",
                "verified": True,
                "changedAt": "2026-08-25T00:00:00Z",
                "actor": "reviewer-x",
            }
        ],
    }
    doc.update(overrides)
    path = tmp_path / "export.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


class TestImportErrors:
    def test_missing_file_exit3(self, out_dir, tmp_path, capsys):
        code = main(
            ["import-corrections", str(tmp_path / "nope.json"), "--output-dir", str(out_dir)]
        )
        assert code == EXIT_INPUT
        assert "不存在" in capsys.readouterr().err

    def test_missing_output_dir_exit3(self, tmp_path, capsys):
        export = _export_file(tmp_path)
        code = main(
            ["import-corrections", str(export), "--output-dir", str(tmp_path / "nope")]
        )
        assert code == EXIT_INPUT
        assert "不存在" in capsys.readouterr().err

    def test_invalid_json_exit3(self, out_dir, tmp_path, capsys):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        code = main(["import-corrections", str(bad), "--output-dir", str(out_dir)])
        assert code == EXIT_INPUT
        assert "JSON" in capsys.readouterr().err

    def test_schema_violation_exit3(self, out_dir, tmp_path, capsys):
        export = _export_file(
            tmp_path,
            changes=[{"entityID": "SH0001", "state": "value", "verified": True}],
        )
        code = main(["import-corrections", str(export), "--output-dir", str(out_dir)])
        assert code == EXIT_INPUT
        assert "field" in capsys.readouterr().err
        assert not (out_dir / "corrections" / "shotAnalysis.json").exists()

    def test_revision_mismatch_rejected(self, out_dir, tmp_path, capsys):
        export = _export_file(tmp_path, sourceRevisionID="deadbeefcafe")
        code = main(["import-corrections", str(export), "--output-dir", str(out_dir)])
        assert code == EXIT_INPUT
        err = capsys.readouterr().err
        assert "outdated" in err or "revision" in err
        assert not (out_dir / "corrections" / "shotAnalysis.json").exists()


class TestImportSuccess:
    def test_valid_import_applies_and_rerenders(self, out_dir, tmp_path, capsys):
        export = _export_file(tmp_path)
        code = main(["import-corrections", str(export), "--output-dir", str(out_dir)])
        assert code == EXIT_OK

        corrections = load_corrections(out_dir, "shotAnalysis")
        assert len(corrections.changes) == 1
        # 导入文件中的 actor 保留
        assert corrections.changes[0]["actor"] == "reviewer-x"
        assert corrections.source_revision_id == FIXTURE_REVISION

        html = (out_dir / "shot-analysis.html").read_text(encoding="utf-8")
        assert 'data-document-status="underReview"' in html
        cell = re.search(
            r'<td data-field="visual\.framing" data-shot-id="SH0001"[^>]*>', html
        ).group(0)
        assert 'data-source="human"' in cell
        assert 'data-original-value="全景"' in cell

    def test_confirmed_fields_merged_idempotent(self, out_dir, tmp_path):
        export = _export_file(
            tmp_path,
            changes=[],
            confirmedAt="2026-08-25T01:00:00Z",
            confirmedBy="reviewer-x",
        )
        argv = ["import-corrections", str(export), "--output-dir", str(out_dir)]
        assert main(argv) == EXIT_OK
        first = load_corrections(out_dir, "shotAnalysis")
        assert first.confirmed_at is not None
        assert first.confirmed_by == "reviewer-x"

        # 重复导入同一文件：保持 confirmed，不报错
        assert main(argv) == EXIT_OK
        second = load_corrections(out_dir, "shotAnalysis")
        assert second.confirmed_at is not None
        assert second.confirmed_by == "reviewer-x"
