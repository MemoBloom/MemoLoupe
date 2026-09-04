"""打包数据目录解析：wheel 内嵌入优先，源码运行回退仓库根。

代码中不得自行拼接仓库根相对路径（``parents[3] / "rules"`` 等）——
源码运行可用，但 pip/brew 安装后 schema/rules/templates 会找不到
（Phase 0.1.1 打包修复）。统一使用 :func:`packaged_path`：

- wheel 安装：数据目录由 hatchling force-include 嵌入包内
  （``memoloupe/schemas``、``memoloupe/rules`` 等），按包内路径解析；
- 源码运行（uv sync / editable）：包内不存在嵌入目录时回退仓库根。
"""

from __future__ import annotations

from pathlib import Path

#: 源码运行时的仓库根（src/memoloupe/core/packaged.py → parents[3]）。
_REPO_ROOT = Path(__file__).resolve().parents[3]
#: 安装后的包根（site-packages/memoloupe 或 src/memoloupe）。
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def packaged_path(*relative: str) -> Path:
    """解析随包分发的数据文件/目录。

    先查包内嵌入位置（wheel 安装），不存在则回退仓库根（源码运行）。
    """
    packaged = _PACKAGE_ROOT.joinpath(*relative)
    if packaged.exists():
        return packaged
    return _REPO_ROOT.joinpath(*relative)
