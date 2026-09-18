"""Watched stretches, the sensor's quarter-hours, and one comparison's accuracy."""

from __future__ import annotations

from datetime import datetime

from crossing_count.export import sensor_accuracy
from crossing_count.manual import intervals_for, merge_ranges, unwatched_ranges

START = datetime(2026, 9, 12, 11, 30, 0)  # noqa: DTZ001 - wall clock as in the filename


def test_watched_stretches_merge_and_gaps_are_listed() -> None:
    w = merge_ranges([(10.0, 20.0), (20.3, 30.0), (0.0, 5.0), (4.0, 6.0)])
    assert w == [[0.0, 6.0], [10.0, 30.0]]
    assert unwatched_ranges(w, 60.0) == [[6.0, 10.0], [30.0, 60.0]]
    assert unwatched_ranges([[0.0, 59.5]], 60.0) == []  # a tail under a second is not a gap


def test_intervals_follow_the_sensors_quarter_hours() -> None:
    (only,) = intervals_for(START, 900.0)
    assert only["key"] == "11:30" and only["full"] is True
    a, b = intervals_for(datetime(2026, 9, 12, 11, 37), 1200.0)  # noqa: DTZ001
    assert (a["key"], a["covered_s"], a["full"]) == ("11:30", 480.0, False)
    assert (b["key"], b["covered_s"], b["full"]) == ("11:45", 720.0, False)
    assert intervals_for(None, 60.0)[0]["key"] == "all"
    # exports run a fraction of a second over: that is not an interval of its own, which the
    # sensor would have to give a number for and nobody counted
    assert [i["key"] for i in intervals_for(START, 900.1)] == ["11:30"]
    assert [i["key"] for i in intervals_for(START, 1800.2)] == ["11:30", "11:45"]
    assert [i["key"] for i in intervals_for(START, 3.0)] == ["11:30"]  # never no interval at all


def test_one_comparison_against_the_sensor() -> None:
    assert sensor_accuracy(None, 11) == {"sensor": None, "verified": 11, "error": None,
                                         "error_pct": None, "accuracy_pct": None}
    got = sensor_accuracy(9, 11)
    assert (got["error"], got["error_pct"], got["accuracy_pct"]) == (-2, -18.2, 81.8)
    # nobody crossed: right only if the sensor also said nobody
    assert sensor_accuracy(0, 0)["accuracy_pct"] == 100.0
    assert sensor_accuracy(3, 0)["accuracy_pct"] is None
    assert sensor_accuracy(30, 10)["accuracy_pct"] == 0.0  # floored, never negative
