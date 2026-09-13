"""Polyline crossing logic, including the degenerate cases."""

import numpy as np
import pytest

from crossing_count.crossing import IN, OUT, find_crossings

LINE = np.array([[0.0, 100.0], [200.0, 100.0]])  # left to right; below (+y) is inside
BENT = np.array([[0.0, 100.0], [100.0, 100.0], [200.0, 140.0]])


def cross(points: list[tuple[float, float]], line: np.ndarray = LINE):  # type: ignore[no-untyped-def]
    return find_crossings([float(i) for i in range(len(points))], points, line, inside_sign=1)


def test_clean_crossing_is_interpolated_in_time_and_space() -> None:
    (c,) = cross([(100, 50), (100, 90), (100, 110), (100, 150)])
    assert c.direction == IN
    assert c.t == pytest.approx(1.5)
    assert (c.x, c.y) == pytest.approx((100, 100))
    assert c.line_position == pytest.approx(0.5)


def test_touching_the_line_without_crossing_is_not_a_crossing() -> None:
    assert cross([(100, 50), (100, 100), (100, 50)]) == []
    assert cross([(100, 50), (100, 100), (100, 100), (120, 60)]) == []


def test_touching_then_continuing_across_is_one_crossing_at_the_touch() -> None:
    (c,) = cross([(100, 50), (100, 100), (100, 150)])
    assert c.direction == IN and c.t == pytest.approx(1.0)


def test_crossing_twice_gives_in_then_out() -> None:
    a, b = cross([(100, 50), (100, 150), (100, 50)])
    assert (a.direction, b.direction) == (IN, OUT)
    assert (a.t, b.t) == pytest.approx((0.5, 1.5))


def test_crossing_exactly_at_a_vertex_counts_once() -> None:
    (c,) = cross([(100, 50), (100, 150)], BENT)
    assert c.direction == IN
    assert (c.x, c.y) == pytest.approx((100, 100))
    assert c.line_position == pytest.approx(100 / (100 + np.hypot(100, 40)))


def test_touching_a_vertex_and_turning_back_is_not_a_crossing() -> None:
    assert cross([(100, 50), (100, 100), (90, 50)], BENT) == []


def test_sample_exactly_on_a_vertex_then_across() -> None:
    (c,) = cross([(100, 50), (100, 100), (110, 130)], BENT)
    assert c.direction == IN and c.t == pytest.approx(1.0)


def test_walking_round_the_end_of_the_line_is_not_a_crossing() -> None:
    assert cross([(250, 50), (250, 150)]) == []
    # ...but the side is still updated, so coming back across the line is an exit.
    (c,) = cross([(250, 50), (250, 150), (100, 150), (100, 50)])
    assert c.direction == OUT


def test_moving_along_the_line_then_leaving_on_the_other_side() -> None:
    (c,) = cross([(50, 50), (50, 100), (150, 100), (150, 150)])
    assert c.direction == IN
    assert (c.x, c.y) == pytest.approx((150, 100))


def test_direction_follows_inside_sign() -> None:
    (c,) = find_crossings([0.0, 1.0], [(100, 50), (100, 150)], LINE, inside_sign=-1)
    assert c.direction == OUT


def test_stationary_samples_do_not_break_crossing_detection() -> None:
    (c,) = cross([(100, 50), (100, 50), (100, 150), (100, 150)])
    assert c.direction == IN and c.t == pytest.approx(1.5)
