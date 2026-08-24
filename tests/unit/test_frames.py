"""media/frames.py 单元测试：代表帧时间夹紧与路径规则（不跑 ffmpeg）。"""

from __future__ import annotations

import pytest

from memoloupe.media.frames import (
    FRAME_EXTRACTION_VERSION,
    frame_file_ref,
    input_cache_key,
    representative_time_ms,
)


def test_version_constant() -> None:
    assert FRAME_EXTRACTION_VERSION == "frames.v1"


def test_midpoint_normal_shot() -> None:
    assert representative_time_ms(0, 3203) == 1601
    assert representative_time_ms(3203, 6400) == 4801


def test_short_shot_clamped_inside() -> None:
    # 39ms 极短镜头：中点仍在区间内部
    assert representative_time_ms(1000, 1039) == 1019


def test_degenerate_one_ms_shot() -> None:
    assert representative_time_ms(500, 501) == 500


def test_never_exact_final_end() -> None:
    for start, end in [(0, 1), (0, 2), (100, 140), (0, 3203), (999, 1000)]:
        t = representative_time_ms(start, end)
        assert start <= t < end, (start, end, t)


def test_empty_range_raises() -> None:
    with pytest.raises(ValueError):
        representative_time_ms(1000, 1000)
    with pytest.raises(ValueError):
        representative_time_ms(1000, 999)


def test_frame_file_ref() -> None:
    assert frame_file_ref("F_SH0001_MAIN") == "evidence/frames/F_SH0001_MAIN.jpg"
    # 一律正斜杠，与平台无关
    assert "\\" not in frame_file_ref("F_SH0002_MAIN")


def test_input_cache_key() -> None:
    assert input_cache_key("a1b2c3d4e5f6") == "original-a1b2"
    assert input_cache_key(None) == "original-unknown"
