"""CLI 分发回归：shot/profile --help 必须显示各自完整帮助。"""

from __future__ import annotations

import pytest

from memoloupe.cli.main import main


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("shot", "--output-dir"),
        ("shot", "--scaffold-only"),
        ("profile", "--skip-distill"),
    ],
)
def test_subcommand_help_shows_full_parser(command, expected, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main([command, "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert expected in out
