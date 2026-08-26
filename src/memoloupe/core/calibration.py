"""core.calibration — 黄金视频校准工具（roadmap 05-03，纯函数）。

提供：

- :func:`load_golden`：加载并校验黄金标注 JSON（格式见
  ``tests/fixtures/golden/README.md``）；
- :func:`boundary_metrics`：检测边界 vs 黄金边界的召回/精确率（容差窗口）；
- :func:`enum_accuracy`：枚举字段逐项匹配准确率；
- :func:`shot_boundary_metrics`：把 shots.json 的边界与黄金标注对比。

没有黄金视频/标注前，这些函数由单元测试锁定语义；真实校准数据到达后，
`tests/e2e/test_golden_calibration.py` 按标注 JSON 驱动 A-001~A-007 回调
（参数变更必须进入 fingerprint，docs/06 记录实证）。
"""

from __future__ import annotations

import json
from pathlib import Path

GOLDEN_SCHEMA_VERSION = 1

#: 黄金标注 JSON 的顶层结构（宽松校验，未知键保留）。
_REQUIRED_TOP = {"schemaVersion", "video", "annotations"}


class GoldenError(ValueError):
    """黄金标注文件不合法。"""


def load_golden(path: Path) -> dict:
    """加载并校验黄金标注 JSON；非法时抛 :class:`GoldenError`。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoldenError(f"黄金标注不可读：{exc}") from None
    if not isinstance(data, dict):
        raise GoldenError("黄金标注必须是 JSON 对象")
    missing = _REQUIRED_TOP - set(data)
    if missing:
        raise GoldenError(f"黄金标注缺顶层字段：{sorted(missing)}")
    if data["schemaVersion"] != GOLDEN_SCHEMA_VERSION:
        raise GoldenError(
            f"schemaVersion 必须为 {GOLDEN_SCHEMA_VERSION}，实际 {data['schemaVersion']!r}"
        )
    annotations = data["annotations"]
    if not isinstance(annotations, dict):
        raise GoldenError("annotations 必须是对象")
    for key in ("shotBoundariesMs", "audioCutPointsMs", "bgmIntervalsMs"):
        if key in annotations and not isinstance(annotations[key], list):
            raise GoldenError(f"annotations.{key} 必须是数组")
    return data


def boundary_metrics(
    detected_ms: list[int], golden_ms: list[int], tolerance_ms: int
) -> dict:
    """检测边界 vs 黄金边界的召回/精确率（容差窗口内算命中）。

    每个黄金边界至多匹配一个检测边界（贪心最近匹配）；返回
    ``{"goldenCount", "detectedCount", "hits", "recall", "precision",
    "missed", "falsePositives"}``。
    """
    golden = sorted(int(g) for g in golden_ms)
    detected = sorted(int(d) for d in detected_ms)
    used = [False] * len(detected)
    hits = 0
    for target in golden:
        best: int | None = None
        for i, d in enumerate(detected):
            if used[i]:
                continue
            if abs(d - target) <= tolerance_ms:
                if best is None or abs(d - target) < abs(detected[best] - target):
                    best = i
        if best is not None:
            used[best] = True
            hits += 1
    return {
        "goldenCount": len(golden),
        "detectedCount": len(detected),
        "hits": hits,
        "recall": hits / len(golden) if golden else 1.0,
        "precision": hits / len(detected) if detected else 1.0,
        "missed": len(golden) - hits,
        "falsePositives": len(detected) - hits,
    }


def enum_accuracy(actual: dict[str, str], golden: dict[str, str]) -> dict:
    """枚举字段逐项匹配：``{"total", "matched", "accuracy"}``。"""
    keys = set(actual) | set(golden)
    matched = sum(1 for k in keys if actual.get(k) == golden.get(k))
    return {
        "total": len(keys),
        "matched": matched,
        "accuracy": matched / len(keys) if keys else 1.0,
    }


def shot_boundary_metrics(
    shots_doc: dict, golden: dict, tolerance_ms: int | None = None
) -> dict:
    """把 shots.json 的镜头边界与黄金标注对比（A-001）。

    检测边界取内部边界（首镜头 finalStartMs 之外的 finalStartMs 集合）；
    黄金边界为 ``annotations.shotBoundariesMs``（含首尾）。容差默认取
    ``tolerances.boundaryMs``，缺省 100。
    """
    shots = shots_doc.get("shots", [])
    if not isinstance(shots, list):
        raise GoldenError("shots.json 不含 shots 数组")
    valid = [
        s for s in shots
        if isinstance(s, dict)
        and isinstance(s.get("finalStartMs"), int)
        and isinstance(s.get("finalEndMs"), int)
    ]
    starts = sorted(int(s["finalStartMs"]) for s in valid)
    if not starts:
        raise GoldenError("shots.json 不含合法镜头")
    last_end = max(int(s["finalEndMs"]) for s in valid)
    # 内部边界：排除首镜头起点与末镜头终点。
    detected_internal = starts[1:]
    golden_ms = golden.get("annotations", {}).get("shotBoundariesMs", [])
    golden_internal = [
        int(g) for g in golden_ms if starts[0] < int(g) < last_end
    ]
    tolerances = golden.get("tolerances", {})
    tolerance = (
        tolerance_ms
        if tolerance_ms is not None
        else int(tolerances.get("boundaryMs", 100))
    )
    return boundary_metrics(detected_internal, golden_internal, tolerance)
