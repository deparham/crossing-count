"""Person detection inside one camera picture.

Two modes, switchable so they can be compared on the same clip:
  naive      YOLO on the whole picture as it is;
  derotated  YOLO on overlapping crops, each rotated so people stand upright
             (see derotate.py), results mapped back and de-duplicated.

Anything the detector finds in the people-free median frame (clothing racks,
mannequins, posters) is remembered as static, and later detections that
closely match one are ignored. A person walking past a rack has a different
box, so they are kept.

Weights are loaded from models/ and are never downloaded at run time.
Ultralytics is forced offline and its usage analytics are switched off.
"""

from __future__ import annotations

import os

os.environ.setdefault("YOLO_OFFLINE", "1")

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np
from numpy.typing import NDArray

from . import derotate as dr
from . import geometry as geo
from .config import BoundGeometry
from .layout import Tile
from .paths import models_dirs

Image = NDArray[np.uint8]
PERSON = 0
STATIC_MIN_CONF = 0.2
STATIC_IOU = 0.6


@dataclass(frozen=True)
class Detection:
    bbox: tuple[float, float, float, float]  # frame pixels, axis-aligned
    foot: tuple[float, float]  # frame pixels
    conf: float
    crop: int = -1  # de-rotation crop it came from (-1: naive)
    quad: tuple[tuple[float, float], ...] = ()  # rotated box corners (empty: the bbox)
    track_box: tuple[float, float, float, float] | None = None  # compact box used to track

    def polygon(self) -> NDArray[np.float32]:
        if self.quad:
            return np.array(self.quad, dtype=np.float32)
        x0, y0, x1, y1 = self.bbox
        return np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float32)


class Detector(Protocol):
    name: str

    def detect(self, frames: Sequence[Image], times: Sequence[float]) -> list[list[Detection]]:
        """Detections per frame, in frame pixel coordinates."""
        ...


def tracking_region(geom: BoundGeometry, tile: Tile, shape: tuple[int, int],
                    line_margin: float = 0.25, zone_margin: float = 0.05,
                    far_margin: float = 0.4) -> NDArray[np.uint8]:
    """Where people are detected and tracked.

    A band around the line, the mask zone, and the parts of filter and exclusion
    zones near the line (a filter zone can cover a whole shop floor; only where
    it meets the line matters for a crossing).
    """
    h = geom.height
    region = geo.rasterize_polyline_band(shape, geom.line, line_margin * h)
    if geom.mask_zone is not None:
        region = geo.u8(cv2.bitwise_or(region, geo.dilate_mask(
            geo.rasterize_polygons(shape, [geom.mask_zone]), zone_margin * h)))
    others = list(geom.filter_zones) + list(geom.exclusion_zones)
    if others:
        near = geo.rasterize_polyline_band(shape, geom.line, far_margin * h)
        zones = geo.dilate_mask(geo.rasterize_polygons(shape, others), zone_margin * h)
        region = geo.u8(cv2.bitwise_or(region, cv2.bitwise_and(zones, near)))
    clip = np.zeros(shape, dtype=np.uint8)
    clip[tile.y0 + tile.header_px : tile.y1, tile.x0 : tile.x1] = 255
    return geo.u8(cv2.bitwise_and(region, clip))


def pick_device(requested: str | None = None) -> str:
    if requested:
        return requested
    import torch

    return "mps" if torch.backends.mps.is_available() else "cpu"


def load_model(name: str) -> Any:
    from ultralytics import YOLO, settings  # type: ignore[attr-defined]

    settings.update({"sync": False})  # type: ignore[no-untyped-call]
    path = Path(name)
    if not path.is_file():
        path = next((d / name for d in models_dirs() if (d / name).is_file()), path)
    if not path.is_file():
        looked = ", ".join(str(d) for d in models_dirs())
        raise FileNotFoundError(
            f"model weights not found: {name} (looked in {looked}). Weights are never "
            f"downloaded automatically; see README for the one-time download."
        )
    return YOLO(str(path))


def quad_iou(a: Detection, b: Detection) -> float:
    ax0, ay0, ax1, ay1 = a.bbox
    bx0, by0, bx1, by1 = b.bbox
    if ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0:
        return 0.0
    pa, pb = a.polygon(), b.polygon()
    inter, _ = cv2.intersectConvexConvex(pa, pb)
    if inter <= 0:
        return 0.0
    union = float(cv2.contourArea(pa)) + float(cv2.contourArea(pb)) - float(inter)
    return float(inter) / union if union > 0 else 0.0


def compact_box(center: tuple[float, float], width: float,
                tile_h: float) -> tuple[float, float, float, float]:
    """A small square on a person's lower body, used for tracking.

    Full outlines of people tilted by the fisheye overlap heavily in a crowd,
    which is where the tracker swaps identities; small lower-body boxes of
    neighbours barely overlap.
    """
    side = float(np.clip(0.6 * width, 0.03 * tile_h, 0.15 * tile_h))
    cx, cy = center
    return (_r(cx - side / 2), _r(cy - side / 2), _r(cx + side / 2), _r(cy + side / 2))


def foot_distance(a: Detection, b: Detection) -> float:
    return float(np.hypot(a.foot[0] - b.foot[0], a.foot[1] - b.foot[1]))


def merge_duplicates(dets: list[Detection], foot_px: float, iou: float = 0.3) -> list[Detection]:
    """Greedy NMS over detections of the same person from overlapping crops."""
    kept: list[Detection] = []
    for d in sorted(dets, key=lambda d: (-d.conf, d.crop, d.bbox)):
        if any(foot_distance(d, k) < foot_px or quad_iou(d, k) > iou for k in kept):
            continue
        kept.append(d)
    return kept


def _r(v: float, nd: int = 1) -> float:
    return round(float(v), nd)


class YoloDetector:
    """Shared YOLO settings. conf is low on purpose: a human reviews every proposal."""

    name = "yolo"

    def __init__(self, model: Any, geom: BoundGeometry, tile: Tile, region: NDArray[np.uint8],
                 device: str, conf: float = 0.1) -> None:
        self.model = model
        self.geom = geom
        self.tile = tile
        self.region = region
        self.device = device
        self.conf = conf
        self.center = geom.fisheye_center
        self.static: list[Detection] = []

    def _predict(self, images: list[Image], imgsz: int) -> list[Any]:
        return list(self.model.predict(images, imgsz=imgsz, conf=self.conf, classes=[PERSON],
                                       device=self.device, verbose=False))

    def _in_region(self, foot: tuple[float, float]) -> bool:
        x, y = int(round(foot[0])), int(round(foot[1]))
        h, w = self.region.shape
        return 0 <= x < w and 0 <= y < h and bool(self.region[y, x])

    def _raw(self, frames: Sequence[Image]) -> list[list[Detection]]:
        raise NotImplementedError

    def learn_static(self, median: Image) -> int:
        """Remember what is 'detected' in the people-free median frame."""
        self.static = [d for d in self._raw([median])[0] if d.conf >= STATIC_MIN_CONF]
        return len(self.static)

    def detect(self, frames: Sequence[Image], times: Sequence[float]) -> list[list[Detection]]:
        out = []
        for dets in self._raw(frames):
            kept = [d for d in dets if not any(quad_iou(d, s) > STATIC_IOU for s in self.static)]
            out.append(sorted(kept, key=lambda d: (d.foot, d.conf)))
        return out


class NaiveDetector(YoloDetector):
    name = "naive"

    def __init__(self, *args: Any, imgsz: int = 640, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.imgsz = imgsz

    def _raw(self, frames: Sequence[Image]) -> list[list[Detection]]:
        t = self.tile
        out: list[list[Detection]] = []
        for res in self._predict([t.crop(f) for f in frames], self.imgsz):
            dets: list[Detection] = []
            if len(res.boxes):
                boxes = res.boxes.xyxy.cpu().numpy()
                confs = res.boxes.conf.cpu().numpy()
                for (x0, y0, x1, y1), c in zip(boxes, confs):
                    bb = (_r(x0 + t.x0), _r(y0 + t.y0), _r(x1 + t.x0), _r(y1 + t.y0))
                    foot = dr.radial_foot_point(bb, self.center)
                    ft = (_r(foot[0]), _r(foot[1]))
                    if self._in_region(ft):
                        mid = ((bb[0] + bb[2]) / 2 + ft[0]) / 2, ((bb[1] + bb[3]) / 2 + ft[1]) / 2
                        tb = compact_box(mid, min(bb[2] - bb[0], bb[3] - bb[1]), t.height)
                        dets.append(Detection(bb, ft, _r(c, 3), track_box=tb))
            out.append(dets)
        return out


class DerotatedDetector(YoloDetector):
    name = "derotated"

    def __init__(self, *args: Any, crop_frac: float = 0.5, imgsz: int = 320, **kw: Any) -> None:
        super().__init__(*args, **kw)
        h = self.tile.height
        self.crop_px = int(round(crop_frac * h))
        self.imgsz = imgsz
        self.specs = dr.plan_crops(self.region, self.center, self.crop_px, self.crop_px // 2,
                                   min_radius_px=0.08 * h)
        self.foot_merge_px = 0.035 * h

    def _raw(self, frames: Sequence[Image]) -> list[list[Detection]]:
        crops = [dr.warp(f, s) for f in frames for s in self.specs]
        results = self._predict(crops, self.imgsz) if crops else []
        n = len(self.specs)
        edge = 3.0
        out: list[list[Detection]] = []
        for fi in range(len(frames)):
            dets: list[Detection] = []
            for si, spec in enumerate(self.specs):
                res = results[fi * n + si]
                if not len(res.boxes):
                    continue
                boxes = res.boxes.xyxy.cpu().numpy()
                confs = res.boxes.conf.cpu().numpy()
                for (x0, y0, x1, y1), c in zip(boxes, confs):
                    conf = float(c)
                    if min(x0, y0) < edge or max(x1, y1) > spec.size - edge:
                        conf *= 0.6  # cut by the crop edge: another crop sees them whole
                    corners = dr.to_frame(spec, np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]]))
                    foot = dr.to_frame(spec, np.array([[(x0 + x1) / 2, y1]]))[0]
                    ft = (_r(foot[0]), _r(foot[1]))
                    if not self._in_region(ft):
                        continue
                    quad = tuple((_r(px), _r(py)) for px, py in corners)
                    bb = (_r(corners[:, 0].min()), _r(corners[:, 1].min()),
                          _r(corners[:, 0].max()), _r(corners[:, 1].max()))
                    lower = dr.to_frame(spec, np.array([[(x0 + x1) / 2, y1 - 0.25 * (y1 - y0)]]))[0]
                    tb = compact_box((float(lower[0]), float(lower[1])), float(x1 - x0),
                                     self.tile.height)
                    dets.append(Detection(bb, ft, _r(conf, 3), spec.index, quad, tb))
            out.append(merge_duplicates(dets, self.foot_merge_px))
        return out
