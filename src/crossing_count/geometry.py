"""Pixel-space geometry for the counting line, mask zone and exclusion zones.

Everything here works in the video's own pixel space. The counting line is burned
into the footage, so no dewarping is ever applied: our geometry must be the
sensor's geometry.

Side convention: for a polyline with vertices v0..vn, a point's side is the sign of
the 2D cross product (v[i+1] - v[i]) x (p - v[i]) against the nearest segment. The
sign is relative to vertex order, so it is well defined for any polyline shape;
"below"/"above"/"left"/"right" in the config are resolved to a sign once.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

Point = tuple[float, float]
FloatArray = NDArray[np.float64]


def u8(a: Any) -> NDArray[np.uint8]:
    """Narrow an OpenCV result to a uint8 image (no copy when it already is one)."""
    return np.asarray(a, dtype=np.uint8)

_IMAGE_DIRECTIONS: dict[str, Point] = {
    # Image coordinates: +y points down the frame.
    "below": (0.0, 1.0),
    "above": (0.0, -1.0),
    "left": (-1.0, 0.0),
    "right": (1.0, 0.0),
}


def to_pixels(points: Sequence[Sequence[float]], width: int, height: int) -> FloatArray:
    """Convert normalised 0-1 coordinates to pixel coordinates, shape (N, 2)."""
    arr = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    return arr * np.array([width, height], dtype=np.float64)


def cross2(a: FloatArray | Point, b: FloatArray | Point) -> float:
    return float(a[0] * b[1] - a[1] * b[0])


def _closest_on_segment(a: FloatArray, b: FloatArray, p: FloatArray) -> tuple[float, float]:
    """Return (t, squared distance) of the closest point on segment ab to p, t in [0, 1]."""
    d = b - a
    denom = float(d @ d)
    t = 0.0 if denom == 0.0 else float(np.clip((p - a) @ d / denom, 0.0, 1.0))
    q = a + t * d
    diff = p - q
    return t, float(diff @ diff)


def distance_to_polyline(polyline: FloatArray, p: Sequence[float] | FloatArray) -> float:
    """Shortest distance from `p` to the polyline (its segments, not their extensions)."""
    pts = np.asarray(polyline, dtype=np.float64)
    q = np.asarray(p, dtype=np.float64)
    return float(np.sqrt(min(_closest_on_segment(pts[i], pts[i + 1], q)[1]
                             for i in range(len(pts) - 1))))


def side_of_polyline(polyline: FloatArray, p: Sequence[float] | FloatArray) -> int:
    """Return +1 / -1 for the side of `p` relative to vertex order, 0 if on the line.

    Uses the nearest segment. When the nearest point is an interior vertex, the
    vertex pseudo-normal (the sum of both adjacent segments' unit normals) decides,
    which gives the correct answer on both the convex and the reflex side of a
    bend. Points beyond an end of the polyline take the side of the end segment
    extended, i.e. the line is treated as continuing straight past its ends.
    """
    pts = np.asarray(polyline, dtype=np.float64)
    q = np.asarray(p, dtype=np.float64)
    n_seg = len(pts) - 1
    best_i, best_t, best_d2 = 0, 0.0, np.inf
    for i in range(n_seg):
        t, d2 = _closest_on_segment(pts[i], pts[i + 1], q)
        if d2 < best_d2 - 1e-12:
            best_i, best_t, best_d2 = i, t, d2
    if best_d2 <= 1e-18:
        return 0

    i = best_i
    at_start_vertex = best_t <= 1e-12 and i > 0
    at_end_vertex = best_t >= 1.0 - 1e-12 and i < n_seg - 1
    if at_start_vertex or at_end_vertex:
        v = i if at_start_vertex else i + 1
        n1 = _unit_left_normal(pts[v - 1], pts[v])
        n2 = _unit_left_normal(pts[v], pts[v + 1])
        s = float((n1 + n2) @ (q - pts[v]))
    else:
        s = cross2(pts[i + 1] - pts[i], q - pts[i])
    if abs(s) < 1e-12:
        return 0
    return 1 if s > 0 else -1


def _unit_left_normal(a: FloatArray, b: FloatArray) -> FloatArray:
    """Normal n with n . (p - a) having the same sign as cross(b - a, p - a)."""
    d = b - a
    n = np.array([-d[1], d[0]], dtype=np.float64)
    length = float(np.hypot(n[0], n[1]))
    return n / length if length > 0 else n


def resolve_inside_sign(polyline: FloatArray, inside_side: str) -> int:
    """Resolve an image-space side word ("below", "left", ...) to +1 / -1.

    Uses the chord from the first to the last vertex. Raises ValueError when the
    word is ambiguous for this line, e.g. "below" for a near-vertical line.
    """
    key = inside_side.strip().lower()
    if key not in _IMAGE_DIRECTIONS:
        raise ValueError(
            f"inside_side must be one of {sorted(_IMAGE_DIRECTIONS)}, got {inside_side!r}"
        )
    pts = np.asarray(polyline, dtype=np.float64)
    chord = pts[-1] - pts[0]
    length = float(np.hypot(chord[0], chord[1]))
    if length == 0.0:
        raise ValueError("counting line's first and last vertices coincide")
    s = cross2(chord / length, _IMAGE_DIRECTIONS[key])
    # |s| is the sine of the angle between the chord and the named direction.
    # Below 0.5 (within 30 degrees) the word does not clearly name a side.
    if abs(s) < 0.5:
        raise ValueError(
            f"inside_side={inside_side!r} is ambiguous for a line whose overall direction "
            f"is ({chord[0]:.3f}, {chord[1]:.3f}); use "
            f"{'left/right' if key in ('below', 'above') else 'below/above'} instead"
        )
    return 1 if s > 0 else -1


def side_word_for_sign(polyline: FloatArray, sign: int) -> str:
    """Inverse of resolve_inside_sign: the unambiguous word naming side `sign`."""
    for word in ("below", "above", "left", "right"):
        try:
            if resolve_inside_sign(polyline, word) == sign:
                return word
        except ValueError:
            continue
    raise ValueError("could not name the inside side of this line")


def polygon_centroid(poly: FloatArray) -> FloatArray:
    """Area centroid of a simple polygon (falls back to vertex mean if degenerate)."""
    pts = np.asarray(poly, dtype=np.float64)
    x, y = pts[:, 0], pts[:, 1]
    x1, y1 = np.roll(x, -1), np.roll(y, -1)
    c = x * y1 - x1 * y
    area = c.sum() / 2.0
    if abs(area) < 1e-12:
        return np.asarray(pts.mean(axis=0), dtype=np.float64)
    return np.array(
        [((x + x1) * c).sum() / (6 * area), ((y + y1) * c).sum() / (6 * area)],
        dtype=np.float64,
    )


def point_in_polygon(poly: FloatArray, p: Sequence[float] | FloatArray) -> bool:
    """Even-odd rule ray cast. Points exactly on an edge may go either way."""
    pts = np.asarray(poly, dtype=np.float64)
    x, y = float(p[0]), float(p[1])
    inside = False
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            x_cross = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
            if x < x_cross:
                inside = not inside
    return inside


def segments_intersect(a: FloatArray, b: FloatArray, c: FloatArray, d: FloatArray) -> bool:
    """True if closed segments ab and cd share any point."""

    def orient(p: FloatArray, q: FloatArray, r: FloatArray) -> float:
        return cross2(q - p, r - p)

    def on_seg(p: FloatArray, q: FloatArray, r: FloatArray) -> bool:
        return bool(
            min(p[0], q[0]) - 1e-12 <= r[0] <= max(p[0], q[0]) + 1e-12
            and min(p[1], q[1]) - 1e-12 <= r[1] <= max(p[1], q[1]) + 1e-12
        )

    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    if ((o1 > 0 and o2 < 0) or (o1 < 0 and o2 > 0)) and (
        (o3 > 0 and o4 < 0) or (o3 < 0 and o4 > 0)
    ):
        return True
    return (
        (abs(o1) < 1e-12 and on_seg(a, b, c))
        or (abs(o2) < 1e-12 and on_seg(a, b, d))
        or (abs(o3) < 1e-12 and on_seg(c, d, a))
        or (abs(o4) < 1e-12 and on_seg(c, d, b))
    )


def polygon_self_intersects(poly: FloatArray) -> bool:
    """True if two non-adjacent edges cross (corners clicked out of order, a 'bow tie')."""
    pts = np.asarray(poly, dtype=np.float64)
    n = len(pts)
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue  # adjacent edges share a corner
            if segments_intersect(pts[i], pts[(i + 1) % n], pts[j], pts[(j + 1) % n]):
                return True
    return False


def polyline_intersects_polygon(polyline: FloatArray, poly: FloatArray) -> bool:
    pl = np.asarray(polyline, dtype=np.float64)
    pg = np.asarray(poly, dtype=np.float64)
    for i in range(len(pl) - 1):
        for j in range(len(pg)):
            if segments_intersect(pl[i], pl[i + 1], pg[j], pg[(j + 1) % len(pg)]):
                return True
    return any(point_in_polygon(pg, v) for v in pl)


def rasterize_polygons(
    shape: tuple[int, int], polygons: Sequence[FloatArray], value: int = 255
) -> NDArray[np.uint8]:
    """Filled uint8 mask of the given pixel-space polygons."""
    mask = np.zeros(shape, dtype=np.uint8)
    for poly in polygons:
        cv2.fillPoly(mask, [np.round(poly).astype(np.int32)], value)
    return mask


def rasterize_polyline_band(
    shape: tuple[int, int], polyline: FloatArray, radius_px: float
) -> NDArray[np.uint8]:
    """Mask of every pixel within `radius_px` of the polyline (Minkowski sum with a disk)."""
    mask = np.zeros(shape, dtype=np.uint8)
    cv2.polylines(mask, [np.round(polyline).astype(np.int32)], False, 255, 1, cv2.LINE_8)
    r = max(0, int(round(radius_px)))
    if r > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
        mask = u8(cv2.dilate(mask, kernel))
    return mask


def dilate_mask(mask: NDArray[np.uint8], radius_px: float) -> NDArray[np.uint8]:
    r = max(0, int(round(radius_px)))
    if r == 0:
        return mask.copy()
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    return u8(cv2.dilate(mask, kernel))
