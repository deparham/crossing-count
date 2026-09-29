"""Find the camera pictures ("tiles") inside a video frame.

RetailNext multi-channel exports place 2-4 camera pictures in one video, each
with a translucent header bar (camera name and clock) over its top rows, inside
black letterboxing. A single-camera export is one tile. Detection runs on a
temporal median frame, so people and moving overlays do not disturb it.

Splitting one camera's picture in two is the costly mistake here, not missing a
join: half a picture gets its own line, its own counting rule and its own row in
the report, and the half without the camera's name in the header looks like a
camera nobody can name. Three things keep that from happening, and each was put
there by an export the one before it could not refuse:

    _seams                the seam holds along the whole of its line, not merely
                          on average. A pillar down part of a picture averages
                          high and stops there.
    _grid                 it falls on an even division, because the pictures in
                          an export are a grid of equal parts. A wall down the
                          whole height of one camera's picture does not.
    _shaped_like_pictures the pieces are still shaped like camera pictures. A
                          line across the middle of a 640x480 picture holds all
                          the way and halves it evenly; 640x239 is the giveaway.
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
GRID_MAX = 4  # the most camera pictures to find in an export by reading the picture alone
# When the channels say how many there are, that guard is not needed and gets in the way:
# CN-323 and YD-407 have five cameras each, and four was the most anything here could split
# into. Guessing five pictures out of the pixels is still refused (GRID_MAX); being told
# five is not a guess.
TOLD_MAX = 6  # the most pictures to cut into when the count is known
TOLD_SPARE = 2  # empty slots a grid may carry: five cameras sit in a 3x2 with one to spare
GRID_SLACK = 0.01  # a seam this far from an even division of the picture is still that one
# A camera picture is roughly as tall as it is wide: every one on hand is 4:3 or 1.2:1.
# Anything far outside that is not a picture but a piece of one.
ASPECT = (0.6, 2.2)


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


def _shaped_like_pictures(xs: list[int], ys: list[int]) -> bool:
    """Whether cutting here leaves pieces shaped like camera pictures.

    The last thing standing between one camera and two. A TC-410 export is one 640x480
    camera with a hard line straight across the middle of its picture: the seam holds down
    its whole length and 240 is an even division of 480, so neither of the other two tests
    can refuse it. The halves give it away - 640x239 is not a camera picture.
    """
    return all(ASPECT[0] <= (b - a) / (d - c) <= ASPECT[1]
               for a, b in zip(xs, xs[1:]) for c, d in zip(ys, ys[1:]) if d > c)


def _cells(g: NDArray[np.float64], xs: list[int], ys: list[int]) -> list[Tile]:
    """The tiles a grid of cuts makes, skipping the empty slots."""
    tiles: list[Tile] = []
    for r in range(len(ys) - 1):
        for c in range(len(xs) - 1):
            cell = g[ys[r] : ys[r + 1], xs[c] : xs[c + 1]]
            if float(cell.mean()) <= BLACK_LEVEL:
                continue  # empty slot in the grid
            tiles.append(Tile(len(tiles), xs[c], ys[r], xs[c + 1], ys[r + 1], _header_rows(cell)))
    return tiles


def _even(length: int, n: int) -> list[int]:
    """Where n equal parts of `length` are cut."""
    return [round(length * k / n) for k in range(1, n)]


def _support(down: NDArray[np.float64], across: NDArray[np.float64],
             gx: list[int], gy: list[int]) -> float:
    """How much the picture itself agrees that these are the joins: the size of the step
    across each cut. Two arrangements can both give the right number of pictures, and this is
    what says which one the export actually has."""
    at = [float(down[c - 1]) for c in gx if 0 < c <= len(down)]
    at += [float(across[c - 1]) for c in gy if 0 < c <= len(across)]
    return sum(at) / len(at) if at else 0.0


def _as_many_as_asked(g: NDArray[np.float64], box: tuple[int, int, int, int], expect: int,
                      down: NDArray[np.float64], across: NDArray[np.float64]) -> list[Tile]:
    """Cut the picture into exactly `expect` camera pictures.

    Used when the export itself says how many cameras it holds, which a RetailNext download
    does: it is asked for named channels and the names are kept beside the file. Then the
    number is not something to work out from the pixels at all, and only the arrangement is -
    two cameras side by side or one above the other, four in a row or in a square. Every
    arrangement that gives the right number is tried and the one the picture agrees with most
    is taken (_support), so a join that is faint still wins over a division with nothing on it.
    """
    x0, y0, x1, y1 = box
    best: tuple[float, list[Tile]] | None = None
    for cols in range(1, TOLD_MAX + 1):
        for rows in range(1, TOLD_MAX + 1):
            # room for every camera, and no more empty slots than a grid really carries
            if not expect <= cols * rows <= expect + TOLD_SPARE:
                continue
            gx, gy = _even(x1 - x0, cols), _even(y1 - y0, rows)
            tiles = _cells(g, [x0, *[x0 + c for c in gx], x1], [y0, *[y0 + c for c in gy], y1])
            if len(tiles) != expect:
                continue
            score = _support(down, across, gx, gy)
            if best is None or score > best[0]:
                best = (score, tiles)
    return best[1] if best else []


def detect_tiles_in(median: Image, expect: int = 0) -> list[Tile]:
    """The camera pictures in one frame. `expect`, when it is known, is how many there are."""
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
    if expect >= 1 and (asked := _as_many_as_asked(g, (x0, y0, x1, y1), expect, down, across)):
        return asked
    cx = [x0 + s for s in _grid(_seams(down, x1 - x0), x1 - x0)]
    cy = [y0 + s for s in _grid(_seams(across, y1 - y0), y1 - y0)]
    # the most pictures whose pieces are still shaped like pictures; one picture last of all
    for gx, gy in ((cx, cy), (cx, []), ([], cy), ([], [])):
        xs, ys = [x0, *gx, x1], [y0, *gy, y1]
        if _shaped_like_pictures(xs, ys):
            break
    return _cells(g, xs, ys)


def detect_tiles(frames: Sequence[Image], expect: int = 0) -> list[Tile]:
    return detect_tiles_in(median_image(frames), expect)
