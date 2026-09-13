"""The counting rule, driven by synthetic tracks (no video)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from crossing_count import rule as R
from crossing_count.crossing import IN, OUT
from crossing_count.tracks import EOF, LOST, Track, TrackSample, stitch

DT = 0.1
LINE = np.array([[0.0, 100.0], [200.0, 100.0]])  # inside is below
MASK = np.array([[20.0, 130.0], [180.0, 130.0], [180.0, 170.0], [20.0, 170.0]])


def params(**kw: Any) -> R.RuleParams:
    base: dict[str, Any] = dict(line=LINE, inside_sign=1, mask_zone=MASK, filter_zones=(),
                                exclusion_zones=(), min_dwell_s=0.4, pending_timeout_s=20.0,
                                frame_dt=DT)
    base.update(kw)
    return R.RuleParams(**base)


def track(keys: list[tuple[float, float, float]], tid: int = 1, ended_by: str = LOST) -> Track:
    """Sample a piecewise-linear path at 10 fps. keys: (t, x, y)."""
    samples = []
    t0, t1 = keys[0][0], keys[-1][0]
    for t in np.round(np.arange(t0, t1 + DT / 2, DT), 3):
        for (ta, xa, ya), (tb, xb, yb) in zip(keys, keys[1:]):
            if ta <= t <= tb:
                f = 0.0 if tb == ta else (t - ta) / (tb - ta)
                x, y = xa + (xb - xa) * f, ya + (yb - ya) * f
                break
        samples.append(TrackSample(float(t), x, y, (x - 15, y - 30, x + 15, y), 0.9))
    return Track(tid, samples, ended_by)


def outcome(tr: Track, **kw: Any) -> R.Outcome:
    return R.evaluate_track(tr, params(**kw))


def reasons(o: R.Outcome) -> list[str]:
    return [r.reason for r in o.rejected]


def test_clean_entry_is_counted_at_the_line() -> None:
    o = outcome(track([(0, 100, 40), (2, 100, 150), (3, 100, 150)]))
    (c,) = o.committed
    assert c.crossing.direction == IN
    assert c.crossing.t == pytest.approx(60 / 110 * 2, abs=0.06)  # stamped at the line
    assert c.commit_t > c.crossing.t  # committed later, when the dwell completed
    assert not o.rejected


def test_threshold_uturn_is_not_counted() -> None:
    o = outcome(track([(0, 100, 40), (1.5, 100, 120), (3, 100, 40)]))
    assert o.committed == []
    assert reasons(o) == [R.UTURN_NO_MASK]


def test_entry_clipping_the_mask_corner_for_less_than_the_dwell_is_not_counted() -> None:
    o = outcome(track([(0, 10, 40), (1, 10, 110), (1.2, 25, 135), (1.4, 10, 160), (2, 10, 200)]))
    assert o.committed == []
    assert reasons(o) == [R.PENDING_EXPIRED]


def test_entry_fragmented_before_the_zone_and_stitched_is_counted() -> None:
    a = track([(0, 100, 40), (1, 100, 110)], tid=1)
    b = track([(1.5, 100, 118), (2.5, 100, 150), (3.5, 100, 150)], tid=2)
    joined, n = stitch([a, b], gap_max_s=1.5, radius_px=40)
    assert n == 1 and len(joined) == 1
    o = R.evaluate_track(joined[0], params())
    (c,) = o.committed
    assert c.crossing.direction == IN and R.FLAG_STITCHED in c.flags


def test_entry_fragmented_and_not_stitched_is_reported_as_expired() -> None:
    a = track([(0, 100, 40), (1, 100, 110)], tid=1)
    b = track([(4.0, 100, 118), (5, 100, 150), (6, 100, 150)], tid=2)  # gap 3 s > 1.5 s
    joined, n = stitch([a, b], gap_max_s=1.5, radius_px=40)
    assert n == 0 and len(joined) == 2
    oa, ob = (R.evaluate_track(t, params()) for t in joined)
    assert reasons(oa) == [R.PENDING_EXPIRED]  # the undercount indicator
    assert ob.committed == [] and ob.rejected == []  # starts inside: no crossing


def test_exit_by_someone_who_entered_before_the_clip_began() -> None:
    o = outcome(track([(0, 100, 150), (1, 100, 150), (2.5, 100, 40)]))
    (c,) = o.committed
    assert c.crossing.direction == OUT


def test_track_still_pending_when_the_footage_ends() -> None:
    o = outcome(track([(0, 100, 40), (1, 100, 115)], ended_by=EOF))
    assert o.committed == [] and reasons(o) == [R.PENDING_AT_EOF]


def test_exit_and_return_on_the_same_track_is_not_counted_and_is_reported() -> None:
    o = outcome(track([(0, 100, 150), (1, 100, 150), (2, 100, 60), (3.5, 100, 150),
                       (4.5, 100, 150)]))
    assert o.committed == []
    (r,) = o.rejected
    assert r.reason == R.RETURNED and r.pattern == "out_in"
    assert [c.direction for c in r.crossings] == [OUT, IN]


def test_entry_that_leaves_again_on_the_same_track_is_not_counted() -> None:
    o = outcome(track([(0, 100, 40), (1.5, 100, 150), (3, 100, 150), (4.5, 100, 40)]))
    assert o.committed == []
    (r,) = o.rejected
    assert r.reason == R.RETURNED and r.pattern == "in_out"


def test_returns_can_be_counted_when_the_config_says_so() -> None:
    o = outcome(track([(0, 100, 150), (1, 100, 150), (2, 100, 60), (3.5, 100, 150),
                       (4.5, 100, 150)]), same_track_returns="count")
    assert [c.crossing.direction for c in o.committed] == [OUT, IN]


def test_hesitating_at_the_threshold_then_entering_counts_once_at_the_last_crossing() -> None:
    o = outcome(track([(0, 100, 40), (1, 100, 115), (2, 100, 80), (3, 100, 150), (4, 100, 150)]))
    (c,) = o.committed
    assert c.crossing.direction == IN and c.crossing.t > 2.0
    assert reasons(o) == [R.UTURN_NO_MASK]
    assert R.FLAG_HAD_RETURN in c.flags


def test_exit_without_having_been_in_the_mask_zone() -> None:
    o = outcome(track([(0, 100, 115), (1, 100, 40)]))
    assert o.committed == [] and reasons(o) == [R.OUTWARD_NO_MASK]


def test_waiting_past_the_pending_timeout_is_not_counted() -> None:
    o = outcome(track([(0, 100, 40), (1, 100, 115), (26, 100, 115), (27, 100, 150),
                       (28, 100, 150)]))
    assert o.committed == [] and reasons(o) == [R.PENDING_EXPIRED]


def test_filter_zone_must_be_touched() -> None:
    near = (np.array([[80.0, 101.0], [120.0, 101.0], [120.0, 200.0], [80.0, 200.0]]),)
    far = (np.array([[150.0, 101.0], [200.0, 101.0], [200.0, 200.0], [150.0, 200.0]]),)
    path = [(0.0, 100.0, 40.0), (2.0, 100.0, 150.0), (3.0, 100.0, 150.0)]
    assert len(outcome(track(path), filter_zones=near).committed) == 1
    o = outcome(track(path), filter_zones=far)
    assert o.committed == [] and reasons(o) == [R.NO_FILTER]


def test_line_only_camera_counts_the_crossing_and_still_reports_returns() -> None:
    assert len(outcome(track([(0, 100, 40), (1, 100, 150)]), mask_zone=None).committed) == 1
    o = outcome(track([(0, 100, 40), (1, 100, 150), (2, 100, 40)]), mask_zone=None)
    assert o.committed == [] and reasons(o) == [R.RETURNED]


def test_exclusion_zone_flags_but_does_not_drop() -> None:
    excl = (np.array([[90.0, 140.0], [110.0, 140.0], [110.0, 160.0], [90.0, 160.0]]),)
    (c,) = outcome(track([(0, 100, 40), (2, 100, 150), (3, 100, 150)]),
                   exclusion_zones=excl).committed
    assert R.FLAG_EXCLUSION in c.flags


THIN = np.array([[20.0, 130.0], [180.0, 130.0], [180.0, 136.0], [20.0, 136.0]])  # 6 px band


def test_walking_through_a_thin_band_needs_zero_dwell() -> None:
    walk = track([(0, 100, 40), (1.0, 100, 190), (2, 100, 190)])  # 15 px per frame
    assert reasons(outcome(walk, mask_zone=THIN)) == [R.PENDING_EXPIRED]
    (c,) = outcome(walk, mask_zone=THIN, min_dwell_s=0.0).committed
    assert c.crossing.direction == IN and c.dwell_s is None
    assert c.dwell_start is not None and c.dwell_start > c.crossing.t


def test_stepping_over_a_thin_band_between_frames_still_passes_through_it() -> None:
    # No sample lands inside the 6 px band: 40 px per frame, samples at y 120 and 160.
    tr = Track(1, [TrackSample(t, 100.0, y, (85, y - 30, 115, y), 0.9)
                   for t, y in ((0.0, 40.0), (0.1, 80.0), (0.2, 120.0), (0.3, 160.0),
                                (0.4, 190.0), (0.5, 190.0))])
    o = R.evaluate_track(tr, params(mask_zone=THIN, min_dwell_s=0.0, smooth_window=1))
    assert len(o.committed) == 1


def test_smoothing_removes_foot_jitter_along_the_line() -> None:
    samples = [TrackSample(round(i * DT, 3), 10.0 + 4 * i, 97.0 + (6.0 if i % 2 else -6.0),
                           (0, 0, 1, 1), 0.9) for i in range(40)]  # walking along, zig-zagging
    tr = Track(1, samples)
    assert R.evaluate_track(tr, params(smooth_window=1)).rejected  # raw: spurious U-turns
    o = R.evaluate_track(tr, params())
    assert o.committed == [] and o.rejected == []


def test_stitching_refuses_wrong_direction_and_distant_tracks() -> None:
    a = track([(0, 100, 40), (1, 100, 110)], tid=1)  # moving down (+y)
    back = track([(1.5, 100, 100), (2.5, 100, 40)], tid=2)  # appears behind, moving up
    far = track([(1.5, 190, 200), (2.5, 190, 250)], tid=3)
    _, n = stitch([a, back], gap_max_s=1.5, radius_px=20)
    assert n == 0
    _, n = stitch([a, far], gap_max_s=1.5, radius_px=20)
    assert n == 0


def test_stitching_never_joins_onto_a_track_that_ended_with_the_footage() -> None:
    a = track([(0, 100, 40), (1, 100, 110)], tid=1, ended_by=EOF)
    b = track([(1.2, 100, 114), (2, 100, 150)], tid=2)
    _, n = stitch([a, b], gap_max_s=1.5, radius_px=40)
    assert n == 0
