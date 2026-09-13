"""Find the camera pictures ("tiles") inside a video frame.

RetailNext multi-channel exports place 2-4 camera pictures in one video, each
with a translucent header bar (camera name and clock) over its top rows, inside
black letterboxing. A single-camera export is one tile. Detection runs on a
temporal median frame, so people and moving overlays do not disturb it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass

import cv2
import numpy as np
from numpy.typing import NDArray

Image = NDArray[np.uint8]

BLACK_LEVEL = 8.0  # mean grey level at or below which a row/column is letterbox


@dataclass(frozen=True)
class Tile:
    index: int  # row-major order: left to right, then top to bottom
    x0: int
    y0: int
    x1: int  # exclusive
    y1: int  # exclusive
    header_px: int  # rows at the top covered by the header bar (camera name, clock)

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    @property
    def origin(self) -> tuple[int, int]:
        return (self.x0, self.y0)

    def crop(self, img: Image) -> Image:
        return img[self.y0 : self.y1, self.x0 : self.x1]

    def contains(self, x: float, y: float) -> bool:
        return self.x0 <= x < self.x1 and self.y0 <= y < self.y1

    def scaled(self, s: float) -> Tile:
        if s == 1.0:
            return self
        return Tile(
            self.index,
            int(round(self.x0 * s)),
            int(round(self.y0 * s)),
            int(round(self.x1 * s)),
            int(round(self.y1 * s)),
            int(round(self.header_px * s)),
        )

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def median_image(frames: Sequence[Image]) -> Image:
    return np.asarray(np.median(np.stack(frames), axis=0), dtype=np.float64).astype(np.uint8)


def _seams(profile: NDArray[np.float64], length: int) -> list[int]:
    """Boundaries (index of the first pixel after the seam) of strong straight seams."""
    if len(profile) < 10:
        return []
    min_part = int(0.15 * length)
    threshold = max(20.0, 2.5 * float(np.percentile(profile, 98)))
    seams: list[int] = []
    for c in np.argsort(-profile, kind="stable"):
        if profile[c] <= threshold:
            break
        b = int(c) + 1
        if b < min_part or length - b < min_part:
            continue
        if all(abs(b - s) >= min_part for s in seams):
            seams.append(b)
    return sorted(seams)


def _header_rows(cell: NDArray[np.float64]) -> int:
    """Height of a dark translucent header bar at the top of a tile, or 0."""
    n = min(48, cell.shape[0] // 4)
    if n < 10:
        return 0
    row_med = np.median(cell[:n], axis=1)  # per-row median: header text is a minority
    for r in range(4, n - 4):
        head = float(np.median(row_med[:r]))
        if head >= 70.0:
            return 0
        first = (float(row_med[r]) + 1.0) / (head + 1.0)
        after = (float(np.median(row_med[r : r + 4])) + 1.0) / (head + 1.0)
        if first >= 1.6 and after >= 1.6:
            return r  # first row where the picture brightens and stays bright
    return 0


def detect_tiles_in(median: Image) -> list[Tile]:
    g = cv2.cvtColor(median, cv2.COLOR_BGR2GRAY).astype(np.float64)
    rows = np.nonzero(g.mean(axis=1) > BLACK_LEVEL)[0]
    cols = np.nonzero(g.mean(axis=0) > BLACK_LEVEL)[0]
    if len(rows) == 0 or len(cols) == 0:
        raise ValueError("frame is entirely black; cannot find camera pictures")
    y0, y1 = int(rows[0]), int(rows[-1]) + 1
    x0, x1 = int(cols[0]), int(cols[-1]) + 1
    region = g[y0:y1, x0:x1]
    xs = [x0] + [x0 + s for s in _seams(np.abs(np.diff(region, axis=1)).mean(0), x1 - x0)] + [x1]
    ys = [y0] + [y0 + s for s in _seams(np.abs(np.diff(region, axis=0)).mean(1), y1 - y0)] + [y1]
    tiles: list[Tile] = []
    for r in range(len(ys) - 1):
        for c in range(len(xs) - 1):
            cell = g[ys[r] : ys[r + 1], xs[c] : xs[c + 1]]
            if float(cell.mean()) <= BLACK_LEVEL:
                continue  # empty slot in the grid
            tiles.append(Tile(len(tiles), xs[c], ys[r], xs[c + 1], ys[r + 1], _header_rows(cell)))
    return tiles


def detect_tiles(frames: Sequence[Image]) -> list[Tile]:
    return detect_tiles_in(median_image(frames))
