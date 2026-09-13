"""Crossings of the counting polyline by a track.

A crossing is a change of side between two samples that are off the line,
where the path between them actually meets the polyline. Samples lying exactly
on the line keep the side they arrived from, so touching the line and turning
back is not a crossing, and passing exactly through a vertex is one crossing,
not two. Walking round the end of the line changes side without meeting it,
so it is not a crossing either.

With a hysteresis, a change of side only counts once the path is at least
that far past the line: foot-point jitter while someone stands at the line
(browsing a display that sits on it) is not a string of crossings. The
crossing is still timed where the path meets the line.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from . import geometry as geo
from .geometry import FloatArray

IN = "in"
OUT = "out"
_EPS = 1e-9


@dataclass(frozen=True)
class Crossing:
    t: float  # interpolated time the path meets the line
    direction: str  # IN or OUT
    x: float  # where it meets the line (frame pixels)
    y: float
    line_position: float  # 0 at the first vertex, 1 at the last, by arc length
    step: int  # path step (sample i -> i + 1) containing the crossing


def _cumulative_length(line: FloatArray) -> FloatArray:
    seg = np.hypot(*np.diff(line, axis=0).T)
    return np.concatenate([[0.0], np.cumsum(seg)])


def _hits(
    path: FloatArray, line: FloatArray, cum: FloatArray, i0: int, i1: int
) -> list[tuple[float, int, float, float, float, float]]:
    """Distinct points where path steps i0..i1-1 meet the line, in path order.

    Each hit is (path parameter, step, u, x, y, arc length along the line).
    """
    found: list[tuple[float, int, float, float, float, float]] = []
    for k in range(i0, i1):
        p, q = path[k], path[k + 1]
        r = q - p
        for j in range(len(line) - 1):
            a, s = line[j], line[j + 1] - line[j]
            den = geo.cross2(r, s)
            if abs(den) < _EPS:
                continue  # stationary or moving along the segment: neighbours catch it
            ap = a - p
            u = geo.cross2(ap, s) / den
            v = geo.cross2(ap, r) / den
            if -_EPS <= u <= 1 + _EPS and -_EPS <= v <= 1 + _EPS:
                u = min(1.0, max(0.0, u))
                v = min(1.0, max(0.0, v))
                x, y = p + u * r
                arc = float(cum[j] + v * (cum[j + 1] - cum[j]))
                found.append((k + u, k, u, float(x), float(y), arc))
    found.sort(key=lambda h: (h[0], h[5]))
    distinct: list[tuple[float, int, float, float, float, float]] = []
    for h in found:  # a vertex is met by both of its segments: keep it once
        if distinct and abs(h[3] - distinct[-1][3]) < 1e-6 and abs(h[4] - distinct[-1][4]) < 1e-6:
            continue
        distinct.append(h)
    return distinct


def find_crossings(
    times: Sequence[float],
    points: Sequence[Sequence[float]] | FloatArray,
    line: FloatArray,
    inside_sign: int,
    hysteresis_px: float = 0.0,
) -> list[Crossing]:
    """All crossings of `line` by the sampled path, in time order."""
    path = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    ln = np.asarray(line, dtype=np.float64)
    if len(path) < 2:
        return []
    cum = _cumulative_length(ln)
    total = float(cum[-1])
    out: list[Crossing] = []
    last_side, last_idx = 0, -1  # confirmed side, and the last sample seen on it
    for i, p in enumerate(path):
        side = geo.side_of_polyline(ln, p)
        if side == 0:
            continue  # on the line: keep the side we arrived from
        if last_side != 0 and side == last_side:
            last_idx = i
            continue
        if last_side != 0 and hysteresis_px > 0 and \
                geo.distance_to_polyline(ln, p) < hysteresis_px:
            continue  # over the line, but not clearly yet
        if last_side != 0:
            hits = _hits(path, ln, cum, last_idx, i)
            if hits:  # otherwise the path went round the end of the line
                _, k, u, x, y, arc = hits[-1]
                t = float(times[k] + u * (times[k + 1] - times[k]))
                out.append(Crossing(
                    t=t,
                    direction=IN if side == inside_sign else OUT,
                    x=x,
                    y=y,
                    line_position=arc / total if total > 0 else 0.0,
                    step=k,
                ))
        last_side, last_idx = side, i
    return out
