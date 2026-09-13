"""Radial de-rotation for ceiling-mounted fisheye pictures.

Seen from a lens pointing straight down, a standing person lies along the
radius through the picture centre: feet toward the centre, head outward.
Directly beneath the lens they are a top-down blob. Stock detectors are trained
on upright people, so each crop is rotated so that its outward radius points
up, which puts the person upright. The crop's bottom-centre then maps back to
the feet. The picture is never dewarped: every coordinate maps back into the
video's own pixel space, the space the burned-in counting line lives in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np
from numpy.typing import NDArray

from .geometry import FloatArray, u8

Image = NDArray[np.uint8]


@dataclass(frozen=True)
class CropSpec:
    index: int
    anchor: tuple[float, float]  # frame pixels
    angle: float  # radians applied: the outward radius ends up pointing up
    size: int  # crop side in frame pixels
    m: FloatArray  # 2x3 affine, frame -> crop
    m_inv: FloatArray  # 2x3 affine, crop -> frame


def _affine(anchor: tuple[float, float], alpha: float, size: int) -> tuple[FloatArray, FloatArray]:
    c, s = math.cos(alpha), math.sin(alpha)
    rot = np.array([[c, -s], [s, c]])
    t = np.array([size / 2, size / 2]) - rot @ np.asarray(anchor)
    m = np.hstack([rot, t[:, None]])
    m_inv = np.hstack([rot.T, (-rot.T @ t)[:, None]])
    return m, m_inv


def upright_angle(anchor: tuple[float, float], center: FloatArray, min_radius: float) -> float:
    """Rotation that maps the direction centre -> anchor onto straight up."""
    vx, vy = anchor[0] - center[0], anchor[1] - center[1]
    if math.hypot(vx, vy) < min_radius:
        return 0.0  # under the lens: no meaningful "outward"
    return -math.pi / 2 - math.atan2(vy, vx)


def plan_crops(
    region: NDArray[np.uint8],
    center: FloatArray,
    crop_px: int,
    step_px: int,
    min_radius_px: float,
) -> list[CropSpec]:
    """Overlapping rotated crops covering every pixel of `region` (a frame-size mask)."""
    ys, xs = np.nonzero(region)
    if len(xs) == 0:
        return []
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    half = crop_px / 2
    specs: list[CropSpec] = []
    gx = np.arange(x0 + step_px / 2, x1 + step_px, step_px)
    gy = np.arange(y0 + step_px / 2, y1 + step_px, step_px)
    for ay in gy:
        for ax in gx:
            wy0, wy1 = max(0, int(ay - half * 0.7)), int(ay + half * 0.7)
            wx0, wx1 = max(0, int(ax - half * 0.7)), int(ax + half * 0.7)
            if not region[wy0:wy1, wx0:wx1].any():
                continue
            anchor = (float(ax), float(ay))
            alpha = upright_angle(anchor, center, min_radius_px)
            m, m_inv = _affine(anchor, alpha, crop_px)
            specs.append(CropSpec(len(specs), anchor, alpha, crop_px, m, m_inv))
    return specs


def warp(frame: Image, spec: CropSpec) -> Image:
    return u8(cv2.warpAffine(frame, spec.m, (spec.size, spec.size), flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0)))


def to_frame(spec: CropSpec, pts: FloatArray) -> FloatArray:
    p = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    return p @ spec.m_inv[:, :2].T + spec.m_inv[:, 2]


def radial_foot_point(bbox: tuple[float, float, float, float], center: FloatArray) -> FloatArray:
    """For an unrotated box: where the ray from its centre toward the lens leaves it.

    Feet sit on the lens side of a person's box, so this approximates the foot
    point without de-rotation. Used by the naive baseline.
    """
    x0, y0, x1, y1 = bbox
    c = np.array([(x0 + x1) / 2, (y0 + y1) / 2])
    d = np.asarray(center, dtype=np.float64) - c
    hw, hh = (x1 - x0) / 2, (y1 - y0) / 2
    if abs(d[0]) <= hw and abs(d[1]) <= hh:
        return c  # the lens is inside the box: a top-down blob, feet under the centre
    scale = min(hw / abs(d[0]) if d[0] else math.inf, hh / abs(d[1]) if d[1] else math.inf)
    return np.asarray(c + d * scale, dtype=np.float64)
