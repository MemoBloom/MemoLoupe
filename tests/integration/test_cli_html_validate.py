"""CLI validate 集成测试：shot-analysis.html 存在时并入 HTML 校验结果。"""

from __future__ import annotations

import shutil
from pathlib import Path

from memoloupe.cli.main import EXIT_OK, EXIT_VALIDATION_FAILED, main
from memoloupe.render.shot_html import render_shot_html

FIXTURE_FULL = Path(__file__).parent.parent / "fixtures" / "output_full"


def _rendered_dir(tmp_path: Path) -> Path:
    work = tmp_path / "out"
    shutil.copytree(FIXTURE_FULL, work)
    render_shot_html(work)
    return work


class TestValidateWithShotHtml:
    def test_rendered_dir_passes(self, tmp_path, capsys):
        work = _rendered_dir(tmp_path)
        assert main(["validate", str(work)]) == EXIT_OK
        out = capsys.readouterr().out
        assert "0 个错误" in out

    def test_rendered_dir_passes_strict(self, tmp_path):
        work = _rendered_dir(tmp_path)
        assert main(["validate", str(work), "--strict"]) == EXIT_OK

    def test_broken_html_fails_with_exit_6(self, tmp_path, capsys):
        work = _rendered_dir(tmp_path)
        html_path = work / "shot-analysis.html"
        text = html_path.read_text(encoding="utf-8")
        # 破坏一个五态单元格：去掉一个 data-verified。
        assert 'data-verified="false"' in text
        html_path.write_text(
            text.replace('data-verified="false"', "", 1), encoding="utf-8"
        )
        code = main(["validate", str(work)])
        assert code == EXIT_VALIDATION_FAILED
        out = capsys.readouterr().out
        assert "shot-analysis.html" in out
        assert "data-verified" in out

    def test_dir_without_html_is_unaffected(self, tmp_path):
        work = tmp_path / "out"
        shutil.copytree(FIXTURE_FULL, work)
        assert main(["validate", str(work)]) == EXIT_OK
