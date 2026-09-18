"""Stretches of footage watched, and the sensor's 15-minute intervals.

The wizard's hand count records which stretches of each camera a person actually watched;
these helpers merge them, say what is left unwatched, and line a video up with the
sensor's own quarter-hours. The standalone hand-counting page that once lived here was
replaced by the wizard's Count by hand step, which keeps the same ideas with a decision
log, gold clips and a report.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

from .export import INTERVAL_MIN, interval_start

MERGE_GAP_S = 0.5  # watched stretches closer than this are one stretch
MIN_GAP_S = 1.0  # unwatched stretches shorter than this are not reported
FULL_TOLERANCE_S = 5.0  # a video covering an interval to within this covers all of it
BRUSHED_S = 5.0  # an interval the footage covers less than this is not compared at all


def merge_ranges(ranges: Iterable[Sequence[float]], gap: float = MERGE_GAP_S) -> list[list[float]]:
    """Union of [start, end] stretches; stretches closer than `gap` become one."""
    spans = sorted((min(float(r[0]), float(r[1])), max(float(r[0]), float(r[1]))) for r in ranges)
    out: list[list[float]] = []
    for a, b in spans:
        if out and a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [[round(a, 2), round(b, 2)] for a, b in out]


def unwatched_ranges(watched: Sequence[Sequence[float]], duration: float,
                     min_gap: float = MIN_GAP_S) -> list[list[float]]:
    """The stretches of [0, duration] not covered by `watched`."""
    gaps: list[list[float]] = []
    pos = 0.0
    for a, b in merge_ranges(watched):
        if a - pos >= min_gap:
            gaps.append([round(pos, 2), round(a, 2)])
        pos = max(pos, b)
    if duration - pos >= min_gap:
        gaps.append([round(pos, 2), round(duration, 2)])
    return gaps


def intervals_for(clock_start: datetime | None, duration: float) -> list[dict[str, Any]]:
    """The sensor's 15-minute intervals this video overlaps, and how much of each it covers.

    An interval the footage only brushes is not one of them: an export a fraction of a second
    past the quarter hour (900.1 s of a 15-minute window) would otherwise need the sensor's
    number for a whole interval nobody counted, and the fetch would fail for want of it."""
    if clock_start is None:
        return [{"key": "all", "label": "whole video", "start": None,
                 "covered_s": round(duration, 1), "full": None}]
    end = clock_start + timedelta(seconds=duration)
    step = timedelta(minutes=INTERVAL_MIN)
    out: list[dict[str, Any]] = []
    s = interval_start(clock_start)
    while s < end:
        e = s + step
        covered = (min(e, end) - max(s, clock_start)).total_seconds()
        out.append({"key": s.strftime("%H:%M"), "label": f"{s:%H:%M} - {e:%H:%M}",
                    "start": s.isoformat(), "covered_s": round(covered, 1),
                    "full": covered >= step.total_seconds() - FULL_TOLERANCE_S})
        s = e
    kept = [i for i in out if float(i["covered_s"]) >= BRUSHED_S]
    return kept or [max(out, key=lambda i: float(i["covered_s"]))]  # never nothing at all
