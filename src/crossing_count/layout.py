"""Find the camera pictures ("tiles") inside a video frame.

RetailNext multi-channel exports place 2-4 camera pictures in one video, each
with a translucent header bar (camera name and clock) over its top rows, inside
black letterboxing. A single-camera export is one tile. Detection runs on a
temporal median frame, so people and moving overlays do not disturb it.

Splitting one camera's picture in two is the costly mistake here, not missing a
join: half a picture gets its own line, its own counting rule and its own row in
the report, and the half without the camera's name in the header looks like a
camera nobody can name. Two things keep that from happening. A seam has to hold
along the whole of its line, not merely on average (_seams), and it has to fall
where an even division of the picture falls, because the pictures in an export
are a grid of equal parts (_grid). Neither is enough alone: an edge inside one
camera's picture can run its whole height and be sharper than the real join.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass

import cv2
import numpy as np
from numpy.typing import NDArray

Image = NDArray[np.uint8]

BLACK_LEVEL = 8.0  # mean grey level at or below which a row/column is letterbox
# A header bar is a translucent dark strip over the picture. Across the exports on hand the
# bars measure 24-70; above this they are not a bar, and the picture starts at the top row.
HEADER_MAX = 100.0
HEADER_LOOK = 12  # rows below a candidate boundary that must all be picture, not more bar
HEADER_RISE = 1.5  # how much brighter than the bar every one of them has to be
GRID_MAX = 4  # the most camera pictures one export holds
GRID_SLACK = 0.01  # a seam this far from an even division of the picture is still that one


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
    """Boundaries (index of the first pixel after the seam) of strong straight seams.

    The profile is a median along the line, never a mean: a join between two pictures differs
    at nearly every row of it, while a pillar or a window frame down part of one picture
    differs at a few rows very strongly and at the rest hardly at all. Measured over the
    exports on hand, a real join's median difference is 47-82 and the strongest thing inside
    a single picture is 3-4, so the floor below is not a close call. The mean did make it
    one: a fisheye entrance with a pillar down the middle scored 22.5 against a threshold of
    22.1 and was split into two cameras.
    """
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
    """Height of a dark translucent header bar at the top of a tile, or 0.

    Being too dark to be picture is a condition on the answer, not a reason to stop looking.
    It used to stop the search, so a bar whose rows grew brighter towards its bottom edge was
    lost the moment the running median passed the cut-off, before the search ever reached the
    bar's real height: a 16-row header measuring 70.5 against a cut-off of 70.0 was read as
    no header at all, leaving a clock that changes every frame inside the tracked picture.
    """
    n = min(48, cell.shape[0] // 4)
    if n < 10:
        return 0
    row_med = np.median(cell[:n], axis=1)  # per-row median: header text is a minority
    for r in range(4, n - 4):
        head = float(np.median(row_med[:r]))
        if head >= HEADER_MAX:
            continue
        # every row below the boundary has to stay brighter than the bar, not just the next
        # few. A bar is not flat: its own top rows can be darker than its body, so "it got
        # brighter here" alone puts the boundary inside the bar and leaves the rest of it,
        # clock and all, in the tracked picture. A picture never dims back to bar level.
        below = row_med[r : r + HEADER_LOOK]
        if float(below.min()) >= HEADER_RISE * (head + 1.0):
            return r
    return 0


def _grid(cuts: list[int], length: int) -> list[int]:
    """Of the seams found, the ones that divide the picture into equal parts.

    The pictures in an export are a grid: 2 side by side, 3 in a row, 2x2. Equal parts, so a
    seam that does not fall on an even division is something inside one camera's picture, not
    a join between two. This is what tells them apart when nothing about the seam itself
    does: a CN-407 export split at 302 as strongly as at its real join at 640, and 302, 338,
    640 is not a grid. The largest division the seams agree with wins, so a missing join
    never turns 3 pictures into 2.
    """
    slack = max(4, int(GRID_SLACK * length))
    for n in range(min(GRID_MAX, len(cuts) + 1), 1, -1):
        taken: list[int] = []
        for k in range(1, n):
            want = round(length * k / n)
            near = sorted(cuts, key=lambda c: abs(c - want))
            if not near or abs(near[0] - want) > slack:
                break
            taken.append(near[0])
        else:
            return taken
    return []


def detect_tiles_in(median: Image) -> list[Tile]:
    g = cv2.cvtColor(median, cv2.COLOR_BGR2GRAY).astype(np.float64)
    rows = np.nonzero(g.mean(axis=1) > BLACK_LEVEL)[0]
    cols = np.nonzero(g.mean(axis=0) > BLACK_LEVEL)[0]
    if len(rows) == 0 or len(cols) == 0:
        raise ValueError("frame is entirely black; cannot find camera pictures")
    y0, y1 = int(rows[0]), int(rows[-1]) + 1
    x0, x1 = int(cols[0]), int(cols[-1]) + 1
    region = g[y0:y1, x0:x1]
    down = np.median(np.abs(np.diff(region, axis=1)), axis=0)  # each column, along its rows
    across = np.median(np.abs(np.diff(region, axis=0)), axis=1)
    xs = [x0] + [x0 + s for s in _grid(_seams(down, x1 - x0), x1 - x0)] + [x1]
    ys = [y0] + [y0 + s for s in _grid(_seams(across, y1 - y0), y1 - y0)] + [y1]
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
