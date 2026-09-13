"""Tracks and short-gap stitching.

Tracks have no long-term identity: the counting rule only needs a track's own
recent path. Stitching joins a track that dies to one that appears shortly
after, close by and moving the same way. Without it, a person briefly occluded
between the line and the mask zone would be discarded as if they had turned
back.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .geometry import FloatArray

LOST = "lost"  # the tracker lost the person mid-range
RANGE_END = "range_end"  # the M1 activity range ended
EOF = "eof"  # the footage (or --sample) ended


@dataclass(frozen=True)
class TrackSample:
    t: float
    x: float  # foot point, frame pixels
    y: float
    bbox: tuple[float, float, float, float]  # frame pixels, x0 y0 x1 y1
    conf: float


@dataclass
class Track:
    track_id: int
    samples: list[TrackSample]
    ended_by: str = LOST
    stitched_from: list[int] = field(default_factory=list)

    @property
    def start_t(self) -> float:
        return self.samples[0].t

    @property
    def end_t(self) -> float:
        return self.samples[-1].t

    def times(self) -> list[float]:
        return [s.t for s in self.samples]

    def points(self) -> FloatArray:
        return np.array([[s.x, s.y] for s in self.samples], dtype=np.float64).reshape(-1, 2)

    def velocity(self, at_end: bool, window_s: float = 1.0) -> FloatArray:
        """Mean velocity (px/s) over the first or last `window_s` of the track."""
        s = self.samples
        if len(s) < 2:
            return np.zeros(2)
        if at_end:
            ref = [x for x in s if x.t >= s[-1].t - window_s]
            a, b = ref[0], s[-1]
        else:
            ref = [x for x in s if x.t <= s[0].t + window_s]
            a, b = s[0], ref[-1]
        dt = b.t - a.t
        if dt <= 0:
            return np.zeros(2)
        return np.array([(b.x - a.x) / dt, (b.y - a.y) / dt])


def split_at_jumps(track: Track, base_px: float, max_speed_px_s: float,
                   new_id: Callable[[], int]) -> list[Track]:
    """Cut a track wherever it moves further than a person can between two samples.

    In a crowd the tracker sometimes hands one person's track to another; the
    foot point then jumps across the picture. Left joined, that jump can look
    like a crossing. Each piece but the last ends LOST, so stitching may rejoin
    genuinely close pieces.
    """
    parts: list[list[TrackSample]] = [[track.samples[0]]]
    for prev, s in zip(track.samples, track.samples[1:]):
        dist = float(np.hypot(s.x - prev.x, s.y - prev.y))
        if dist > base_px + max_speed_px_s * (s.t - prev.t):
            parts.append([s])
        else:
            parts[-1].append(s)
    if len(parts) == 1:
        return [track]
    out: list[Track] = []
    for i, samples in enumerate(parts):
        last = i == len(parts) - 1
        out.append(Track(track.track_id if i == 0 else new_id(), samples,
                         track.ended_by if last else LOST,
                         list(track.stitched_from) if i == 0 else []))
    return out


def _compatible(a: Track, b: Track, gap_max_s: float, radius_px: float,
                max_speed_px_s: float) -> float | None:
    """Cost of joining b onto a, or None if they cannot be the same person."""
    if a.ended_by != LOST:
        return None
    gap = b.start_t - a.end_t
    if gap <= 0 or gap > gap_max_s:
        return None
    la, fb = a.samples[-1], b.samples[0]
    disp = np.array([fb.x - la.x, fb.y - la.y])
    dist = float(np.hypot(*disp))
    va = a.velocity(at_end=True)
    speed = float(np.hypot(*va))
    # Foot jitter inflates the measured speed; never allow more than walking speed.
    if dist > radius_px + min(2.0 * speed, max_speed_px_s) * gap:
        return None
    if speed > 5.0:  # moving: the new track must continue roughly the same way
        if dist > radius_px / 2 and float(va @ disp) <= 0:
            return None
        vb = b.velocity(at_end=False)
        if float(np.hypot(*vb)) > 5.0 and float(va @ vb) <= 0:
            return None
    return gap + dist / max(radius_px, 1.0)


def stitch(tracks: list[Track], gap_max_s: float, radius_px: float,
           max_speed_px_s: float = float("inf")) -> tuple[list[Track], int]:
    """Join short gaps greedily (cheapest first). Returns (tracks, number of joins)."""
    ordered = sorted(tracks, key=lambda t: (t.start_t, t.track_id))
    by_start = [t.start_t for t in ordered]
    pairs: list[tuple[float, int, int]] = []
    for i, a in enumerate(ordered):
        lo = bisect.bisect_right(by_start, a.end_t)
        hi = bisect.bisect_right(by_start, a.end_t + gap_max_s)
        for j in range(lo, hi):
            if i != j:
                cost = _compatible(a, ordered[j], gap_max_s, radius_px, max_speed_px_s)
                if cost is not None:
                    pairs.append((cost, i, j))
    pairs.sort()
    successor: dict[int, int] = {}
    has_pred: set[int] = set()
    for _, i, j in pairs:
        if i in successor or j in has_pred:
            continue
        successor[i] = j
        has_pred.add(j)

    out: list[Track] = []
    for i, t in enumerate(ordered):
        if i in has_pred:
            continue
        merged = Track(t.track_id, list(t.samples), t.ended_by, list(t.stitched_from))
        k = i
        while k in successor:
            k = successor[k]
            nxt = ordered[k]
            merged.samples.extend(nxt.samples)
            merged.stitched_from.append(nxt.track_id)
            merged.ended_by = nxt.ended_by
        out.append(merged)
    return out, len(successor)
