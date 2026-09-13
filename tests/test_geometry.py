import numpy as np
import pytest

from crossing_count import geometry as geo

H_LINE = np.array([[0.0, 0.0], [10.0, 0.0]])
V_SHAPE = np.array([[0.0, 0.0], [10.0, 10.0], [20.0, 0.0]])  # image coords: the V opens upward


def test_side_of_straight_line_below_is_positive_for_left_to_right() -> None:
    assert geo.side_of_polyline(H_LINE, (5, 3)) == 1  # +y is down the image
    assert geo.side_of_polyline(H_LINE, (5, -3)) == -1


def test_side_on_the_line_is_zero() -> None:
    assert geo.side_of_polyline(H_LINE, (5, 0)) == 0
    assert geo.side_of_polyline(V_SHAPE, (10, 10)) == 0


def test_side_beyond_the_ends_extends_the_end_segment() -> None:
    assert geo.side_of_polyline(H_LINE, (15, 3)) == 1
    assert geo.side_of_polyline(H_LINE, (-5, -3)) == -1


def test_side_at_a_vertex_uses_the_pseudo_normal() -> None:
    # Directly below the V's vertex (outside the V) and directly above it (inside the V).
    assert geo.side_of_polyline(V_SHAPE, (10, 12)) == 1
    assert geo.side_of_polyline(V_SHAPE, (10, 5)) == -1
    # Points hugging each arm stay consistent with that arm.
    assert geo.side_of_polyline(V_SHAPE, (5, 7)) == 1
    assert geo.side_of_polyline(V_SHAPE, (15, 3)) == -1


def test_resolve_inside_sign() -> None:
    assert geo.resolve_inside_sign(H_LINE, "below") == 1
    assert geo.resolve_inside_sign(H_LINE, "above") == -1
    assert geo.resolve_inside_sign(H_LINE[::-1], "below") == -1
    vertical = np.array([[0.0, 0.0], [0.0, 10.0]])
    assert geo.resolve_inside_sign(vertical, "right") == geo.side_of_polyline(vertical, (3, 5))


def test_resolve_inside_sign_rejects_ambiguous_words() -> None:
    vertical = np.array([[0.0, 0.0], [0.0, 10.0]])
    with pytest.raises(ValueError, match="ambiguous"):
        geo.resolve_inside_sign(vertical, "below")
    with pytest.raises(ValueError):
        geo.resolve_inside_sign(H_LINE, "inward")


def test_side_word_round_trip() -> None:
    for line in (H_LINE, H_LINE[::-1], np.array([[0.0, 0.0], [1.0, 9.0]])):
        for sign in (1, -1):
            assert geo.resolve_inside_sign(line, geo.side_word_for_sign(line, sign)) == sign


def test_point_in_polygon_and_centroid() -> None:
    sq = np.array([[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]])
    assert geo.point_in_polygon(sq, (2, 2))
    assert not geo.point_in_polygon(sq, (5, 2))
    assert np.allclose(geo.polygon_centroid(sq), [2, 2])


def test_segments_intersect_cases() -> None:
    a, b = np.array([0.0, 0.0]), np.array([4.0, 4.0])
    assert geo.segments_intersect(a, b, np.array([0.0, 4.0]), np.array([4.0, 0.0]))  # X
    assert geo.segments_intersect(a, b, np.array([4.0, 4.0]), np.array([6.0, 0.0]))  # shared end
    assert not geo.segments_intersect(a, b, np.array([0.0, 1.0]), np.array([4.0, 5.0]))  # parallel
    assert geo.segments_intersect(a, b, np.array([2.0, 2.0]), np.array([6.0, 6.0]))  # collinear


def test_polyline_band_radius() -> None:
    m = geo.rasterize_polyline_band((50, 50), np.array([[5.0, 25.0], [45.0, 25.0]]), 6)
    assert m[25 + 5, 25] == 255
    assert m[25 + 8, 25] == 0
