"""时间与区间工具（docs/02 §1.2）。

- 业务时间一律为整数毫秒，字段以 ``Ms`` 结尾。
- 区间统一为半开区间 ``[startMs, endMs)``；点证据允许 ``startMs == endMs``。
- 秒 → 毫秒必须使用本模块的 :func:`seconds_to_ms`
  （``decimal.Decimal`` + ROUND_HALF_UP），禁止各模块自行 ``int()``。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# 区间表示为 (startMs, endMs) 整数二元组，半开 [startMs, endMs)。
Range = tuple[int, int]

_MS_PER_SECOND = Decimal(1000)


def seconds_to_ms(seconds: float | str | Decimal) -> int:
    """项目唯一的秒 → 毫秒转换。

    使用 ``Decimal`` 并按 ROUND_HALF_UP 舍入到最近毫秒，
    同一 ffprobe 时间戳在各文件中必须得到相同整数。
    支持 float、Decimal 以及 ``"3.203"`` 这类 ffprobe 字符串输出。
    """
    if isinstance(seconds, Decimal):
        value = seconds
    elif isinstance(seconds, float):
        # 经 repr 走字符串，避免 0.1+0.2 类二进制浮点尾差进入舍入。
        value = Decimal(repr(seconds))
    elif isinstance(seconds, str):
        try:
            value = Decimal(seconds.strip())
        except InvalidOperation as exc:
            raise ValueError(f"非法秒数字符串: {seconds!r}") from exc
    else:
        value = Decimal(seconds)
    ms = value * _MS_PER_SECOND
    return int(ms.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_fraction(text: str) -> float | None:
    """解析 ``"30000/1001"`` 形式的帧率；分母为 0 或格式非法返回 None。

    也接受纯数字字符串（如 ``"25"``）。
    """
    if not isinstance(text, str):
        return None
    parts = text.strip().split("/")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            numerator = float(parts[0])
            denominator = float(parts[1])
            if denominator == 0:
                return None
            return numerator / denominator
    except ValueError:
        return None
    return None


def overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """两个半开区间是否有正交集（``end == start`` 不算重叠）。"""
    return a_start < b_end and b_start < a_end


def intersection_ms(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    """两个半开区间的交集长度（毫秒），不相交时为 0。"""
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def contains(range_: Range, point_ms: int) -> bool:
    """区间是否包含时间点。

    普通区间为 ``start <= point < end``；点证据（``start == end``）
    仅在 ``point == start`` 时成立。
    """
    start, end = range_
    if start == end:
        return point_ms == start
    return start <= point_ms < end


def is_contiguous(sorted_ranges: list[Range]) -> bool:
    """已按 start 升序的区间序列是否相邻相接（前一 end == 后一 start）。"""
    for prev, curr in zip(sorted_ranges, sorted_ranges[1:]):
        if prev[1] != curr[0]:
            return False
    return True


def assert_non_overlapping(ranges: list[Range]) -> None:
    """断言区间序列两两无重叠（点证据不参与重叠判定），否则抛 ValueError。"""
    ordered = sorted(ranges)
    for i, (a_start, a_end) in enumerate(ordered):
        if a_end < a_start:
            raise ValueError(f"区间 end < start: {ordered[i]!r}")
        if a_start == a_end:
            continue  # 点证据不构成重叠
        for b_start, b_end in ordered[i + 1 :]:
            if b_start >= a_end:
                break
            if b_start == b_end:
                continue  # 点证据不构成重叠
            if overlaps(a_start, a_end, b_start, b_end):
                raise ValueError(
                    f"区间重叠: ({a_start}, {a_end}) 与 ({b_start}, {b_end})"
                )
