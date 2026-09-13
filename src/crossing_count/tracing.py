"""Pure helpers behind trace_line.py, kept apart from the GUI so they can be tested."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from . import geometry as geo
from .config import ConfigError, bind, parse_config

Point = tuple[float, float]


def dedupe(points: Sequence[Point], min_dist: float = 0.75) -> list[Point]:
    """Drop consecutive points closer than `min_dist` (double clicks)."""
    out: list[Point] = []
    for p in points:
        if not out or np.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) >= min_dist:
            out.append((float(p[0]), float(p[1])))
    return out


def normalise(points: Sequence[Point], width: int, height: int) -> list[list[float]]:
    return [
        [round(min(1.0, max(0.0, x / width)), 4), round(min(1.0, max(0.0, y / height)), 4)]
        for x, y in points
    ]


def build_config_dict(
    *,
    site: str,
    sensor: str,
    tile_size: tuple[int, int],
    line: Sequence[Point],
    inside_point: Point,
    mask_zone: Sequence[Point] | None,
    filter_zones: Sequence[Sequence[Point]],
    exclusion_zones: Sequence[Sequence[Point]],
    gate_ignore_zones: Sequence[Sequence[Point]],
    fps: float,
    overlay_hsv: tuple[int, int, int] | None,
    traced_on: dict[str, Any],
    min_dwell_in_zone_s: float = 0.4,
    pending_timeout_s: float = 20.0,
    stitch_gap_max_s: float = 1.5,
    notes: str = "",
    gate: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Build and validate a config from picture-local pixel points.

    Returns (config dict, warnings). Raises ConfigError if it is inconsistent.
    """
    w, h = tile_size
    line_px = np.asarray(dedupe(line), dtype=np.float64)
    if len(line_px) < 2:
        raise ConfigError("the counting line needs at least 2 distinct points")
    sign = geo.side_of_polyline(line_px, inside_point)
    if sign == 0:
        raise ConfigError("the inside point is on the line; click clearly on the store side")
    try:
        word = geo.side_word_for_sign(line_px, sign)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    raw: dict[str, Any] = {
        "site": site,
        "sensor": sensor,
        "line": normalise([tuple(p) for p in line_px], w, h),
        "inside_side": word,
    }
    if mask_zone:
        raw["mask_zone"] = normalise(dedupe(mask_zone), w, h)
    raw["min_dwell_in_zone_s"] = min_dwell_in_zone_s
    raw["pending_timeout_s"] = pending_timeout_s
    raw["stitch_gap_max_s"] = stitch_gap_max_s
    raw["filter_zones"] = [normalise(dedupe(z), w, h) for z in filter_zones]
    raw["exclusion_zones"] = [normalise(dedupe(z), w, h) for z in exclusion_zones]
    if gate_ignore_zones:
        raw["gate_ignore_zones"] = [normalise(dedupe(z), w, h) for z in gate_ignore_zones]
    raw["fps_assumed"] = round(float(fps), 3)
    if overlay_hsv is not None:
        raw["overlay_hsv"] = [int(c) for c in overlay_hsv]
    raw["traced_on"] = traced_on
    raw["notes"] = notes
    if gate:
        raw["gate"] = gate

    cfg = parse_config(raw)
    geom = bind(cfg, w, h)
    return raw, list(geom.warnings)


def inside_point_for(line: Sequence[Point], sign: int, distance: float = 20.0) -> Point:
    """A point `distance` px off the middle of the line on side `sign` (for --edit)."""
    pts = np.asarray(line, dtype=np.float64)
    i = (len(pts) - 1) // 2
    a, b = pts[i], pts[i + 1]
    d = b - a
    n = np.array([-d[1], d[0]]) / max(1e-9, float(np.hypot(d[0], d[1])))
    p = (a + b) / 2 + n * sign * distance
    return (float(p[0]), float(p[1]))
