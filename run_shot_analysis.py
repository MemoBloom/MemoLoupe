"""根级薄包装：``python run_shot_analysis.py INPUT --output-dir DIR``。

业务实现在 :func:`memoloupe.cli.shot_analysis.run_shot_analysis`（docs/01 §3/§10）。
"""

from __future__ import annotations

import sys

from memoloupe.cli.shot_analysis import run_shot_analysis

if __name__ == "__main__":
    raise SystemExit(run_shot_analysis(sys.argv[1:]))
