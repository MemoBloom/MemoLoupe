"""根级薄包装：``python run_profile_build.py --output-dir DIR``。

业务实现在 :func:`memoloupe.cli.profile_build.run_profile_build`（docs/01 §3/§10）。
"""

from __future__ import annotations

import sys

from memoloupe.cli.profile_build import run_profile_build

if __name__ == "__main__":
    raise SystemExit(run_profile_build(sys.argv[1:]))
