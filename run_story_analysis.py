"""根级薄包装：``python run_story_analysis.py --output-dir DIR``。

业务实现在 :func:`memoloupe.cli.story_analysis.run_story_analysis`（docs/01 §3/§10）。
"""

from __future__ import annotations

import sys

from memoloupe.cli.story_analysis import run_story_analysis

if __name__ == "__main__":
    raise SystemExit(run_story_analysis(sys.argv[1:]))
