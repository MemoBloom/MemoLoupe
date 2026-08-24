"""time_ranges 模块单元测试。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from memoloupe.core.time_ranges import (
    assert_non_overlapping,
    contains,
    intersection_ms,
    is_contiguous,
    overlaps,
    parse_fraction,
    seconds_to_ms,
)


class TestSecondsToMs:
    def test_basic(self) -> None:
        assert seconds_to_ms(3.203) == 3203
        assert seconds_to_ms(Decimal("3.203")) == 3203
        assert seconds_to_ms("3.203") == 3203

    def test_ffprobe_style_string(self) -> None:
        assert seconds_to_ms("60.064") == 60064

    def test_float_trap_01_plus_02(self) -> None:
        # 0.1 + 0.2 = 0.30000000000000004，必须稳定舍入为 300
        assert seconds_to_ms(0.1 + 0.2) == 300

    def test_round_half_up(self) -> None:
        assert seconds_to_ms("0.0005") == 1
        assert seconds_to_ms("0.0004") == 0
        assert seconds_to_ms("1.2345") == 1235

    def test_negative_half_up(self) -> None:
        assert seconds_to_ms("-0.0005") == -1

    def test_invalid_string_raises(self) -> None:
        with pytest.raises(ValueError):
            seconds_to_ms("not-a-number")


class TestParseFraction:
    def test_ntsc_frame_rate(self) -> None:
        value = parse_fraction("30000/1001")
        assert value is not None
        assert value == pytest.approx(29.97003, abs=1e-5)

    def test_plain_number(self) -> None:
        assert parse_fraction("25") == 25.0

    def test_zero_denominator_returns_none(self) -> None:
        assert parse_fraction("30000/0") is None

    def test_invalid_format_returns_none(self) -> None:
        assert parse_fraction("abc") is None
        assert parse_fraction("1/2/3") is None
        assert parse_fraction("") is None


class TestRanges:
    def test_overlaps_positive(self) -> None:
        assert overlaps(0, 100, 50, 200)
        assert overlaps(50, 200, 0, 100)

    def test_half_open_touching_not_overlap(self) -> None:
        # end == start 不算重叠
        assert not overlaps(0, 100, 100, 200)
        assert not overlaps(100, 200, 0, 100)

    def test_intersection_ms(self) -> None:
        assert intersection_ms(0, 100, 50, 200) == 50
        assert intersection_ms(0, 100, 100, 200) == 0
        assert intersection_ms(0, 100, 300, 400) == 0

    def test_contains(self) -> None:
        assert contains((0, 100), 0)
        assert contains((0, 100), 99)
        assert not contains((0, 100), 100)  # 半开区间不含 end

    def test_point_evidence_range(self) -> None:
        # 点证据 start == end 合法
        assert contains((500, 500), 500)
        assert not contains((500, 500), 499)

    def test_is_contiguous(self) -> None:
        assert is_contiguous([(0, 100), (100, 200), (200, 350)])
        assert not is_contiguous([(0, 100), (101, 200)])
        assert is_contiguous([])
        assert is_contiguous([(0, 100)])

    def test_assert_non_overlapping_ok(self) -> None:
        assert_non_overlapping([(0, 100), (100, 200)])
        assert_non_overlapping([(200, 300), (0, 100)])  # 不要求有序

    def test_assert_non_overlapping_raises(self) -> None:
        with pytest.raises(ValueError):
            assert_non_overlapping([(0, 150), (100, 200)])

    def test_assert_non_overlapping_allows_point_evidence(self) -> None:
        # 点证据落在区间内不构成区间重叠
        assert_non_overlapping([(0, 100), (50, 50), (100, 200)])

    def test_assert_non_overlapping_rejects_inverted(self) -> None:
        with pytest.raises(ValueError):
            assert_non_overlapping([(100, 50)])
