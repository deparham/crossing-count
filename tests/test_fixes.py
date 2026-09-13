"""Fixes for busy scenes: hysteresis at the line, splitting swapped tracks,
walking-speed stitching, crossings made out of sight, duplicate counts,
tracks broken at the line, compact tracking boxes and faint detections."""

from __future__ import annotations

import numpy as np
import pytest

from crossing_count import rule as R
from crossing_count.candidates import FLAG_BIG_JUMP, MISS_KIND, build_outputs
from crossing_count.config import bind, parse_config
from crossing_count.crossing import IN, find_crossings
from crossing_count.detector import Detection, compact_box
from crossing_count.tracker import ByteTrackAdapter
from crossing_count.tracks import LOST, Track, TrackSample, split_at_jumps, stitch

LINE = np.array([[0.0, 100.0], [200.0, 100.0]])  # inside is below


def samples(keys: list[tuple[float, float, float]], conf: float = 0.9) -> list[TrackSample]:
    """Piecewise-linear path sampled at 10 fps. keys: (t, x, y)."""
    out = []
    for t in np.round(np.arange(keys[0][0], keys[-1][0] + 0.05, 0.1), 3):
        for (ta, xa, ya), (tb, xb, yb) in zip(keys, keys[1:]):
            if ta <= t <= tb:
                f = 0.0 if tb == ta else (t - ta) / (tb - ta)
                x, y = xa + (xb - xa) * f, ya + (yb - ya) * f
                break
        out.append(TrackSample(float(t), x, y, (x - 15, y - 30, x + 15, y), conf))
    return out


def track(keys: list[tuple[float, float, float]], tid: int = 1, conf: float = 0.9) -> Track:
    return Track(tid, samples(keys, conf), LOST)


def test_hysteresis_ignores_jitter_at_the_line() -> None:
    t = [i * 0.1 for i in range(30)]
    pts = [(50 + 4 * i, 100 + (8 if i % 2 else -8)) for i in range(30)]
    assert len(find_crossings(t, pts, LINE, 1)) > 5
    assert find_crossings(t, pts, LINE, 1, hysteresis_px=15) == []


def test_hysteresis_keeps_real_crossings_and_their_timing() -> None:
    (c,) = find_crossings([0.0, 1.0, 2.0, 3.0], [(100, 50), (100, 90), (100, 110), (100, 150)],
                          LINE, 1, hysteresis_px=15)
    assert c.direction == IN and c.t == pytest.approx(1.5)


def test_hysteresis_from_a_standing_start_on_the_line() -> None:
    (c,) = find_crossings([0.0, 1.0, 2.0, 3.0], [(100, 99.2), (100, 99.2), (100, 130), (100, 160)],
                          LINE, 1, hysteresis_px=15)
    assert c.direction == IN


def test_a_swapped_track_is_split_at_the_jump() -> None:
    s = samples([(0, 100, 40), (1, 100, 90)]) + samples([(1.1, 100, 300), (2, 100, 350)])
    whole = Track(1, s)
    assert len(find_crossings(whole.times(), whole.points(), LINE, 1)) == 1  # a fake crossing
    ids = iter(range(100, 200))
    parts = split_at_jumps(whole, base_px=48, max_speed_px_s=288, new_id=lambda: next(ids))
    assert [p.track_id for p in parts] == [1, 100]
    assert parts[0].ended_by == LOST
    assert all(find_crossings(p.times(), p.points(), LINE, 1) == [] for p in parts)


def test_walking_is_not_split() -> None:
    walk = track([(0, 100, 40), (2, 100, 300)])  # 130 px/s
    assert split_at_jumps(walk, 48, 288, lambda: 99) == [walk]


def test_stitching_refuses_joins_faster_than_walking() -> None:
    a = track([(0, 0, 100), (1, 50, 100)])  # 50 px/s
    far = track([(1.5, 350, 100), (2, 360, 100)], tid=2)
    near = track([(1.5, 75, 100), (2, 85, 100)], tid=3)
    assert stitch([a, far], 1.5, 40, max_speed_px_s=240)[1] == 0
    assert stitch([a, near], 1.5, 40, max_speed_px_s=240)[1] == 1


def test_crossing_made_out_of_sight_is_flagged() -> None:
    s = samples([(0, 100, 40), (1, 100, 80)]) + samples([(2, 100, 150), (3, 100, 160)])
    rp = R.RuleParams(line=LINE, inside_sign=1, mask_zone=None, filter_zones=(),
                      exclusion_zones=(), min_dwell_s=0, pending_timeout_s=20, smooth_window=1)
    (c,) = R.evaluate_track(Track(1, s), rp).committed
    assert R.FLAG_UNSEEN in c.flags


def test_duplicates_are_merged_and_broken_tracks_are_listed_as_likely_misses() -> None:
    cfg = parse_config({"site": "S", "sensor": "CAM", "inside_side": "below", "fps_assumed": 10,
                        "line": [[100 / 640, 200 / 480], [540 / 640, 200 / 480]]})
    geom = bind(cfg, 640, 480)
    tracks = [
        track([(0, 300, 150), (2, 300, 260)], tid=1, conf=0.9),
        track([(0.3, 305, 150), (2.3, 305, 260)], tid=2, conf=0.5),  # same person, split track
        track([(5, 450, 120), (6, 450, 190)], tid=3),  # lost just short of the line ...
        track([(6.6, 452, 212), (8, 452, 300)], tid=4),  # ... and found just past it
    ]
    times = [round(i * 0.1, 1) for i in range(100)]
    act = {"processed_duration_s": 20.0, "motion_runs": [{"start_s": 0.0, "end_s": 10.0}]}
    cand, disc, unex = build_outputs(cfg, geom, act, tracks, times, [1] * len(times), 0, 0, 0.1,
                                     {"picture": {}})
    (c,) = cand["candidates"]
    assert c["track_id"] == 1 and c["direction"] == "in"
    (d,) = disc["discarded"]
    assert d["reason"] == R.DUPLICATE and d["duplicate_of"] == c["id"] and d["track_id"] == 2
    miss = unex["unexplained"][0]
    assert miss["kind"] == MISS_KIND and miss["direction_guess"] == "in"
    assert miss["tracks"] == [3, 4]
    assert cand["summary"]["duplicates"] == 1
    assert cand["summary"]["likely_missed_crossings"] == 1


def test_a_track_lost_just_past_the_line_is_listed_as_a_likely_miss() -> None:
    cfg = parse_config({"site": "S", "sensor": "CAM", "inside_side": "below", "fps_assumed": 10,
                        "line": [[100 / 640, 200 / 480], [540 / 640, 200 / 480]]})
    geom = bind(cfg, 640, 480)
    lost = track([(0, 300, 150), (1, 300, 210)], tid=7)  # 10 px past: under the 14 px threshold
    act = {"processed_duration_s": 10.0, "motion_runs": [{"start_s": 0.0, "end_s": 3.0}]}
    cand, disc, unex = build_outputs(cfg, geom, act, [lost], [0.0], [1], 0, 0, 0.1,
                                     {"picture": {}})
    assert cand["candidates"] == [] and disc["discarded"] == []
    miss = unex["unexplained"][0]
    assert miss["kind"] == MISS_KIND and miss["direction_guess"] == "in" and miss["tracks"] == [7]
    assert "past the line" in miss["detail"]


def test_a_big_jump_near_the_crossing_is_flagged_not_dropped() -> None:
    cfg = parse_config({"site": "S", "sensor": "CAM", "inside_side": "below", "fps_assumed": 10,
                        "line": [[100 / 640, 200 / 480], [540 / 640, 200 / 480]]})
    geom = bind(cfg, 640, 480)
    glued = Track(1, samples([(0, 300, 150), (1, 300, 190)])  # one person up to the line ...
                  + samples([(1.1, 420, 215), (2, 420, 300)]), LOST)  # ... 123 px away: another
    walk = track([(10, 200, 150), (12, 200, 300)], tid=2)
    times = [round(i * 0.1, 1) for i in range(150)]
    act = {"processed_duration_s": 20.0, "motion_runs": [{"start_s": 0.0, "end_s": 15.0}]}
    cand, _, _ = build_outputs(cfg, geom, act, [glued, walk], times, [1] * len(times), 0, 0, 0.1,
                               {"picture": {}})
    first, second = cand["candidates"]
    assert first["track_id"] == 1 and FLAG_BIG_JUMP in first["flags"]
    assert second["track_id"] == 2 and FLAG_BIG_JUMP not in second["flags"]
    assert cand["summary"]["flagged_big_jump"] == 1


def test_compact_tracking_box_is_small_and_centred() -> None:
    x0, y0, x1, y1 = compact_box((200.0, 300.0), width=40.0, tile_h=480)
    assert x1 - x0 == pytest.approx(24.0)
    assert ((x0 + x1) / 2, (y0 + y1) / 2) == pytest.approx((200.0, 300.0))
    tiny = compact_box((0.0, 0.0), width=5.0, tile_h=480)
    assert tiny[2] - tiny[0] == pytest.approx(0.03 * 480)


def test_tracker_thresholds_decide_whether_faint_people_start_tracks() -> None:
    d = Detection((100, 100, 130, 160), (115, 160), 0.18)
    strict = ByteTrackAdapter(analysed_fps=10, high=0.25, new=0.25)
    assert not any(strict.update([d]) for _ in range(3))
    lenient = ByteTrackAdapter(analysed_fps=10, high=0.15, new=0.15)
    assert any(lenient.update([d]) for _ in range(3))


def test_tracker_can_associate_on_either_box() -> None:
    d = Detection((100, 100, 136, 190), (118, 190), 0.6, track_box=compact_box((118, 170), 36, 480))
    for box in ("full", "compact"):
        bt = ByteTrackAdapter(analysed_fps=10, box=box, high=0.2, new=0.2)
        assert any(bt.update([d]) for _ in range(3))
    with pytest.raises(ValueError):
        ByteTrackAdapter(analysed_fps=10, box="huge")
