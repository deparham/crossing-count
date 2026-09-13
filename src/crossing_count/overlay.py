"""The burned-in counting-line overlay, used as a fingerprint.

When a camera is traced, the colour of the burned-in line under the traced
vertices is recorded. Later stages check that the config's line still sits on
pixels of that colour. This identifies which picture in a multi-camera export
belongs to which config, and catches a sensor whose line was moved after the
config was traced (counting against a stale line would silently bias every
result).

Thin overlay lines lose most of their saturation to 4:2:0 chroma subsampling,
so the colour is found by contrast: the hue over-represented on the line
compared with a band just beside it.
"""

from __future__ import annotations

import cv2
import numpy as np
from numpy.typing import NDArray

from . import geometry as geo
from .geometry import FloatArray

HSV = tuple[int, int, int]
Image = NDArray[np.uint8]

MIN_SAT = 35
MIN_VAL = 50

# RetailNext draws its counting line and zones as thin lines, light blue to dark blue.
MARK_HUES = (80, 130)  # any colour a drawing's recorded line may have
MARK_BANDS = (  # (hue low, hue high, min saturation, min value)
    (80, 112, 60, 90),  # the light-blue counting line and zones
    (100, 130, 80, 40),  # the dark-blue lines
)
MARK_MIN_SEGMENTS = 6
# Per 640-pixel-wide picture, light and dark lines together: exports with marks had
# 1100-2600 px of such lines (Carindale's mostly dark blue); clean ones at most 305 px.
MARK_MIN_LENGTH_PX = 600.0


def counting_overlay_evidence(img: Image) -> dict[str, float | int | bool]:
    """Does a camera picture show the sensor's burned-in lines?

    Looks, in a people-free picture, for thin straight lines in RetailNext's blues.
    Thick blue things (signs, clothes, displays) are removed first, so only
    line-like marks count.
    """
    scale = img.shape[1] / 640.0
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    n, total = 0, 0.0
    for lo, hi, smin, vmin in MARK_BANDS:
        m = ((h >= lo) & (h <= hi) & (s >= smin) & (v >= vmin)).astype(np.uint8) * 255
        thin = cv2.subtract(m, cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8)))
        segs = cv2.HoughLinesP(thin, 1, np.pi / 180, threshold=30,
                               minLineLength=max(20.0, 40 * scale), maxLineGap=6)
        arr = (np.asarray(segs, dtype=np.float64).reshape(-1, 4) if segs is not None
               else np.zeros((0, 4)))
        lengths = np.hypot(arr[:, 2] - arr[:, 0], arr[:, 3] - arr[:, 1])
        n, total = n + int(len(lengths)), total + float(lengths.sum())
    return {"segments": n, "length_px": round(total, 1),
            "marks": n >= MARK_MIN_SEGMENTS and total >= MARK_MIN_LENGTH_PX * scale}


def sample_polyline(line: FloatArray, step: float = 2.0) -> FloatArray:
    pts: list[FloatArray] = []
    arr = np.asarray(line, dtype=np.float64)
    for a, b in zip(arr[:-1], arr[1:]):
        n = max(1, int(np.ceil(float(np.hypot(*(b - a))) / step)))
        for k in range(n):
            pts.append(a + (b - a) * (k / n))
    pts.append(arr[-1])
    return np.asarray(pts, dtype=np.float64)


def _hue_distance(h: NDArray[np.int64], h0: int) -> NDArray[np.int64]:
    d = np.abs(h - h0)
    return np.asarray(np.minimum(d, 180 - d), dtype=np.int64)


def _hue_fraction(hues: NDArray[np.int64], n_pixels: int) -> NDArray[np.float64]:
    hist = np.bincount(hues, minlength=180).astype(np.float64)
    # Circular smoothing over +-3 hue steps.
    smooth = np.sum([np.roll(hist, k) for k in range(-3, 4)], axis=0)
    return np.asarray(smooth / max(1, n_pixels), dtype=np.float64)


def overlay_color_from_line(
    img: Image, line: FloatArray, on_radius: int = 3, off_radii: tuple[int, int] = (7, 11)
) -> HSV | None:
    """Colour of the burned-in line under a traced polyline, or None if there is none."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.int64)
    shape = img.shape[:2]
    on = geo.rasterize_polyline_band(shape, line, on_radius) > 0
    near = geo.rasterize_polyline_band(shape, line, off_radii[0]) > 0
    off = (geo.rasterize_polyline_band(shape, line, off_radii[1]) > 0) & ~near
    coloured = (hsv[..., 1] >= MIN_SAT) & (hsv[..., 2] >= MIN_VAL)
    f_on = _hue_fraction(hsv[..., 0][on & coloured], int(on.sum()))
    f_off = _hue_fraction(hsv[..., 0][off & coloured], int(off.sum()))
    diff = f_on - f_off
    peak = int(np.argmax(diff))
    if diff[peak] < 0.08:
        return None
    sel = on & coloured & (_hue_distance(hsv[..., 0], peak) <= 4)
    if not sel.any():
        return None
    return (peak, int(np.median(hsv[..., 1][sel])), int(np.median(hsv[..., 2][sel])))


def color_mask(img: Image, hsv0: HSV, hue_tol: int = 7) -> NDArray[np.uint8]:
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.int64)
    h0, s0, v0 = hsv0
    m = (
        (_hue_distance(hsv[..., 0], h0) <= hue_tol)
        & (hsv[..., 1] >= max(MIN_SAT, int(0.5 * s0)))
        & (hsv[..., 2] >= max(MIN_VAL, int(0.5 * v0)))
    )
    return m.astype(np.uint8) * 255


def line_match_score(img: Image, line: FloatArray, hsv0: HSV, radius: int = 3) -> float:
    """Fraction of points along `line` with an overlay-coloured pixel within `radius`."""
    mask = color_mask(img, hsv0)
    if radius > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
        mask = geo.u8(cv2.dilate(mask, k))
    h, w = mask.shape
    samples = sample_polyline(line)
    hits = 0
    for x, y in samples:
        xi, yi = int(round(x)), int(round(y))
        if 0 <= xi < w and 0 <= yi < h and mask[yi, xi]:
            hits += 1
    return hits / len(samples)
