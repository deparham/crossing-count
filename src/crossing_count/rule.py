"""The counting rule. Track-scoped: a crossing is judged against the whole track.

1. Find the track's crossings of the line (on a lightly smoothed foot path).
2. A crossing followed by one in the opposite direction on the same track
   cancels: the person went out and came back (or in and back out). Neither
   counts, and the pair is reported. `same_track_returns: "count"` in the site
   config turns this off, so that every crossing counts.
3. Each crossing left over is a candidate, subject to the camera's zones:
   - mask zone, inward: after crossing, the track must reach the mask zone within
     `pending_timeout_s` and stay in it for `min_dwell_in_zone_s`. With a dwell
     of 0, passing through is enough - right for thin RetailNext mask bands;
   - mask zone, outward: before crossing, the track must have been in the mask
     zone (for `min_dwell_in_zone_s`, or at all when that is 0);
   - filter zones: the track must touch one somewhere on its path.

"In a zone" is tested on the path between samples too, so someone who steps
over a thin band between two frames still passes through it.

Counts are timestamped at the line crossing, never at the moment of commitment.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from . import geometry as geo
from .config import BoundGeometry, SiteConfig
from .crossing import IN, OUT, Crossing, find_crossings
from .geometry import FloatArray
from .tracks import EOF, RANGE_END, Track

# Discard reasons.
UTURN_NO_MASK = "uturn_no_mask"  # in, then back out without reaching the mask zone
RETURNED = "returned_same_track"  # out then back in, or in (past the mask) then out
PENDING_EXPIRED = "pending_expired"  # in, but lost or timed out before the mask zone
PENDING_AT_EOF = "pending_at_eof"  # in, still waiting for the mask zone when footage ended
PENDING_AT_RANGE_END = "pending_at_range_end"  # same, when the M1 range ended
OUTWARD_NO_MASK = "outward_no_mask"  # out, without having been in the mask zone
NO_FILTER = "no_filter"  # never touched a filter zone

DUPLICATE = "duplicate"  # same direction, same place, moments apart, on a split track

# Flags on committed candidates.
FLAG_EXCLUSION = "exclusion_zone"
FLAG_STITCHED = "stitched"
FLAG_HAD_RETURN = "track_also_returned"
FLAG_UNSEEN = "crossed_while_unseen"  # the tracker bridged a gap across the line
UNSEEN_GAP_S = 0.5


@dataclass(frozen=True)
class RuleParams:
    line: FloatArray
    inside_sign: int
    mask_zone: FloatArray | None
    filter_zones: tuple[FloatArray, ...]
    exclusion_zones: tuple[FloatArray, ...]
    min_dwell_s: float
    pending_timeout_s: float
    same_track_returns: str = "flag"
    frame_dt: float = 0.1
    dwell_gap_s: float = 0.5  # sample gaps up to this still count as continuous presence
    smooth_window: int = 5  # samples in the centred moving average of the foot path
    hysteresis_px: float = 0.0  # how far past the line a change of side must go to count

    @classmethod
    def from_config(cls, cfg: SiteConfig, geom: BoundGeometry, frame_dt: float) -> RuleParams:
        return cls(
            line=geom.line,
            inside_sign=geom.inside_sign,
            mask_zone=geom.mask_zone,
            filter_zones=geom.filter_zones,
            exclusion_zones=geom.exclusion_zones,
            min_dwell_s=cfg.min_dwell_in_zone_s,
            pending_timeout_s=cfg.pending_timeout_s,
            same_track_returns=cfg.same_track_returns,
            frame_dt=frame_dt,
            smooth_window=max(1, int(round(0.5 / frame_dt))),
            hysteresis_px=0.03 * geom.height,
        )


@dataclass
class Committed:
    track_id: int
    crossing: Crossing
    commit_t: float  # when the rule was satisfied (>= crossing time)
    dwell_s: float | None
    flags: list[str] = field(default_factory=list)
    dwell_start: float | None = None  # when the track reached the mask zone


@dataclass
class Rejected:
    track_id: int
    reason: str
    crossings: list[Crossing]
    pattern: str | None = None  # "in_out" / "out_in" for returns
    flags: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    committed: list[Committed]
    rejected: list[Rejected]


def smooth_path(pts: FloatArray, window: int) -> FloatArray:
    """Centred moving average; the window shrinks at the ends so they stay put."""
    if window <= 1 or len(pts) < 3:
        return pts
    half = window // 2
    out = np.empty_like(pts)
    for i in range(len(pts)):
        out[i] = pts[max(0, i - half) : i + half + 1].mean(axis=0)
    return out


def touch_times(times: Sequence[float], pts: FloatArray, poly: FloatArray) -> list[float]:
    """Times the path is inside `poly`, including passing through it between samples."""
    inside = [geo.point_in_polygon(poly, p) for p in pts]
    n, m = len(pts), len(poly)
    out: list[float] = []
    for i in range(n):
        if inside[i]:
            out.append(times[i])
        elif i + 1 < n and not inside[i + 1]:
            a, b = pts[i], pts[i + 1]
            if any(geo.segments_intersect(a, b, poly[j], poly[(j + 1) % m]) for j in range(m)):
                out.append((times[i] + times[i + 1]) / 2)
    return out


def _dwell_runs(times: list[float], inside: list[bool], gap_s: float,
                dt: float) -> list[tuple[float, float]]:
    """(start, end) of continuous presence; end includes one frame interval."""
    runs: list[tuple[float, float]] = []
    start: float | None = None
    prev = 0.0
    for t, ins in zip(times, inside):
        if ins:
            if start is None or t - prev > gap_s:
                if start is not None:
                    runs.append((start, prev + dt))
                start = t
            prev = t
        elif start is not None:
            runs.append((start, prev + dt))
            start = None
    if start is not None:
        runs.append((start, prev + dt))
    return runs


def evaluate_track(track: Track, rp: RuleParams) -> Outcome:
    times = track.times()
    pts = smooth_path(track.points(), rp.smooth_window)
    crossings = find_crossings(times, pts, rp.line, rp.inside_sign, rp.hysteresis_px)
    if not crossings:
        return Outcome([], [])
    dt = rp.frame_dt

    # Mask presence as intervals: dwell runs, or instants of passing through.
    visits: list[tuple[float, float]] = []
    if rp.mask_zone is not None:
        if rp.min_dwell_s > 0:
            inside = [geo.point_in_polygon(rp.mask_zone, p) for p in pts]
            visits = [r for r in _dwell_runs(times, inside, rp.dwell_gap_s, dt)
                      if r[1] - r[0] >= rp.min_dwell_s - 1e-9]
        else:
            visits = [(t, t) for t in touch_times(times, pts, rp.mask_zone)]
    touched_filter = not rp.filter_zones or any(
        touch_times(times, pts, z) for z in rp.filter_zones)

    flags: list[str] = []
    if track.stitched_from:
        flags.append(FLAG_STITCHED)
    if any(touch_times(times, pts, z) for z in rp.exclusion_zones):
        flags.append(FLAG_EXCLUSION)

    # Cancel opposite pairs on the same track.
    stack: list[Crossing] = []
    pairs: list[tuple[Crossing, Crossing]] = []
    for c in crossings:
        if rp.same_track_returns == "flag" and stack and stack[-1].direction != c.direction:
            pairs.append((stack.pop(), c))
        else:
            stack.append(c)

    rejected: list[Rejected] = []
    for a, b in pairs:
        if a.direction == IN:
            reached = any(s >= a.t - dt and e <= b.t + dt for s, e in visits)
            reason = UTURN_NO_MASK if rp.mask_zone is not None and not reached else RETURNED
            rejected.append(Rejected(track.track_id, reason, [a, b], "in_out", list(flags)))
        else:
            rejected.append(Rejected(track.track_id, RETURNED, [a, b], "out_in", list(flags)))
    if pairs:
        flags.append(FLAG_HAD_RETURN)

    committed: list[Committed] = []
    for c in stack:
        cf = list(flags)
        if times[c.step + 1] - times[c.step] > UNSEEN_GAP_S:
            cf.append(FLAG_UNSEEN)
        if not touched_filter:
            rejected.append(Rejected(track.track_id, NO_FILTER, [c], None, cf))
            continue
        if rp.mask_zone is None:
            committed.append(Committed(track.track_id, c, c.t, None, cf))
            continue
        if c.direction == IN:
            after = [(s, e) for s, e in visits if s >= c.t - dt]
            if after and after[0][0] + rp.min_dwell_s - c.t <= rp.pending_timeout_s:
                s0, e0 = after[0]
                dwell = round(e0 - s0, 3) if rp.min_dwell_s > 0 else None
                committed.append(Committed(track.track_id, c, max(c.t, s0 + rp.min_dwell_s),
                                           dwell, cf, s0))
                continue
            if after or track.end_t - c.t >= rp.pending_timeout_s:
                reason = PENDING_EXPIRED  # timed out
            elif track.ended_by == EOF:
                reason = PENDING_AT_EOF
            elif track.ended_by == RANGE_END:
                reason = PENDING_AT_RANGE_END
            else:
                reason = PENDING_EXPIRED  # lost before reaching the mask zone
            rejected.append(Rejected(track.track_id, reason, [c], None, cf))
        else:
            before = [(s, e) for s, e in visits if e <= c.t + dt]
            if before:
                s0, e0 = before[-1]
                dwell = round(e0 - s0, 3) if rp.min_dwell_s > 0 else None
                committed.append(Committed(track.track_id, c, c.t, dwell, cf, s0))
            else:
                rejected.append(Rejected(track.track_id, OUTWARD_NO_MASK, [c], None, cf))
    return Outcome(committed, rejected)


__all__ = ["IN", "OUT", "Committed", "Outcome", "Rejected", "RuleParams", "evaluate_track"]
