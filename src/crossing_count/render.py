"""Debug and preview drawing. Output images are derived from CCTV: they go under
runs/ (gitignored), never into sites/.

Our colours are chosen to differ from the RetailNext overlay (light blue, dark
blue, grey, pink) so the reviewer can tell our geometry from the sensor's."""

from __future__ import annotations

from collections.abc import Sequence

import cv2
import numpy as np
from numpy.typing import NDArray

from .config import BoundGeometry
from .util import fmt_hms

Image = NDArray[np.uint8]

LINE = (255, 0, 255)  # magenta
MASK = (0, 220, 0)  # green
FILTER = (0, 140, 255)  # orange
EXCLUSION = (0, 0, 255)  # red
IGNORE = (160, 160, 160)  # grey
ROI_TINT = (255, 140, 0)


def _poly(img: Image, pts: NDArray[np.float64], color: tuple[int, int, int], closed: bool) -> None:
    cv2.polylines(img, [np.round(pts).astype(np.int32)], closed, color, 1, cv2.LINE_AA)


def draw_geometry(img: Image, geom: BoundGeometry, roi: NDArray[np.uint8] | None = None) -> Image:
    out = img.copy()
    if roi is not None:
        sel = roi > 0
        tint = np.array(ROI_TINT, dtype=np.float32)
        out[sel] = (out[sel].astype(np.float32) * 0.65 + tint * 0.35).astype(np.uint8)
    for z in geom.gate_ignore_zones:
        _poly(out, z, IGNORE, True)
    for z in geom.exclusion_zones:
        _poly(out, z, EXCLUSION, True)
    for z in geom.filter_zones:
        _poly(out, z, FILTER, True)
    if geom.mask_zone is not None:
        _poly(out, geom.mask_zone, MASK, True)
    _poly(out, geom.line, LINE, False)
    for v in geom.line:
        cv2.circle(out, (int(round(v[0])), int(round(v[1]))), 2, LINE, -1, cv2.LINE_AA)
    # Arrow from the middle of the line toward the inside.
    i = (len(geom.line) - 1) // 2
    a, b = geom.line[i], geom.line[i + 1]
    mid = (a + b) / 2
    d = b - a
    n = np.array([-d[1], d[0]]) / max(1e-9, float(np.hypot(d[0], d[1])))
    tip = mid + n * geom.inside_sign * 0.06 * geom.height
    cv2.arrowedLine(out, (int(mid[0]), int(mid[1])), (int(tip[0]), int(tip[1])), LINE, 1,
                    cv2.LINE_AA, tipLength=0.3)
    cv2.putText(out, "in", (int(tip[0]) + 3, int(tip[1]) + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                LINE, 1)
    return out


def label(img: Image, text: str, color: tuple[int, int, int] = (0, 255, 255)) -> Image:
    out = img.copy()
    (w, h), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
    y = out.shape[0] - h - 8
    cv2.rectangle(out, (0, y), (w + 8, out.shape[0]), (0, 0, 0), -1)
    cv2.putText(out, text, (4, out.shape[0] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1,
                cv2.LINE_AA)
    return out


def contact_sheet(tiles: Sequence[Image], cols: int = 4, tile_width: int = 400) -> Image:
    if not tiles:
        return np.zeros((40, tile_width, 3), dtype=np.uint8)
    h0, w0 = tiles[0].shape[:2]
    th = int(round(h0 * tile_width / w0))
    resized = [cv2.resize(t, (tile_width, th), interpolation=cv2.INTER_AREA) for t in tiles]
    while len(resized) % cols:
        resized.append(np.zeros_like(resized[0]))
    rows = [np.hstack(resized[i : i + cols]) for i in range(0, len(resized), cols)]
    return np.vstack(rows)


def timeline_image(
    times: NDArray[np.float64],
    values: NDArray[np.float64],
    threshold: float,
    ranges: Sequence[tuple[float, float]],
    duration: float,
    warmup_s: float,
    width: int = 1600,
    height: int = 260,
) -> Image:
    """Per-second peak blob area on a log scale, with active ranges shaded."""
    img = np.full((height, width, 3), 255, dtype=np.uint8)
    top, bottom = 10, height - 30

    def x_of(t: float) -> int:
        return int(round(t / max(duration, 1e-9) * (width - 1)))

    vmax = max(float(values.max()) if len(values) else 1.0, threshold * 4, 10.0)

    def y_of(v: float) -> int:
        frac = np.log10(1 + v) / np.log10(1 + vmax)
        return int(round(bottom - frac * (bottom - top)))

    for s, e in ranges:
        cv2.rectangle(img, (x_of(s), top), (x_of(e), bottom), (200, 240, 200), -1)
    cv2.rectangle(img, (0, top), (x_of(warmup_s), bottom), (220, 220, 220), -1)
    if len(times):
        sec = np.floor(times).astype(np.int64)
        peak = np.zeros(int(sec.max()) + 1)
        np.maximum.at(peak, sec, values)
        pts = np.array([[x_of(i + 0.5), y_of(v)] for i, v in enumerate(peak)], dtype=np.int32)
        cv2.polylines(img, [pts], False, (60, 60, 60), 1, cv2.LINE_AA)
    ty = y_of(threshold)
    cv2.line(img, (0, ty), (width - 1, ty), (0, 0, 220), 1)
    cv2.putText(img, f"min blob {threshold:.0f}px", (6, ty - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                (0, 0, 220), 1)
    step = 300 if duration > 1800 else 60 if duration > 300 else 10
    t = 0.0
    while t <= duration:
        x = x_of(t)
        cv2.line(img, (x, bottom), (x, bottom + 4), (0, 0, 0), 1)
        cv2.putText(img, fmt_hms(t)[:8], (x + 2, height - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.35,
                    (0, 0, 0), 1)
        t += step
    return img
