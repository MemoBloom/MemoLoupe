"""CLI validate 集成测试：shot-analysis.html 存在时并入 HTML 校验结果。"""

from __future__ import annotations

import html as html_lib
import json
import re
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


class TestReviewReasonsRegression:
    """resolver→render→validate 回归（roadmap 03-01）：

    resolver 产生的 needsReview 理由经渲染进入 data-review-reasons，
    严格校验必须全程通过；人为破坏一致性必须被捕获。
    """

    def test_resolver_reasons_survive_render_and_strict_validate(self, tmp_path):
        work = _rendered_dir(tmp_path)
        assert main(["validate", str(work), "--strict"]) == EXIT_OK
        html_text = (work / "shot-analysis.html").read_text(encoding="utf-8")
        header = next(
            line for line in html_text.splitlines()
            if 'scope="col" data-shot-id="SH0002"' in line
        )
        # SH0002：camera-motion 与模型语义冲突，理由须机器可读地保留到页面。
        assert 'data-needs-review="true"' in header
        match = re.search(r'data-review-reasons="([^"]*)"', header)
        assert match
        reasons = json.loads(html_lib.unescape(match.group(1)))
        assert any("不一致" in r for r in reasons)

    def test_tampered_needs_review_fails_strict(self, tmp_path):
        work = _rendered_dir(tmp_path)
        html_path = work / "shot-analysis.html"
        text = html_path.read_text(encoding="utf-8")
        header = next(
            line for line in text.splitlines()
            if 'scope="col" data-shot-id="SH0002"' in line
        )
        tampered = header.replace('data-needs-review="true"', 'data-needs-review="false"')
        assert tampered != header
        html_path.write_text(text.replace(header, tampered), encoding="utf-8")
        assert main(["validate", str(work), "--strict"]) == EXIT_VALIDATION_FAILED
