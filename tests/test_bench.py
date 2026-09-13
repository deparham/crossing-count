"""The benchmark: where each verified crossing would reach the checker, from recorded detections."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from synth import Oracle

from crossing_count.bench import CHECKED, HAND, bench_clip, find_truth, score_camera, totals
from crossing_count.candidates import DetectOptions, RecordingDetector, run_detect, write_detection
from crossing_count.gating import camera_dir


def test_each_verified_crossing_is_put_where_the_checker_would_meet_it() -> None:
    real = [(10.0, "in"), (20.0, "in"), (30.0, "out"), (40.0, "in"), (50.0, "in")]
    items: list[dict[str, Any]] = [
        {"kind": "counted", "direction": "in", "t": 10.5},
        {"kind": "counted", "direction": "in", "t": 11.0},   # the same person again
        {"kind": "counted", "direction": "in", "t": 70.0},   # nobody
        {"kind": "counted", "direction": "out", "t": 20.2},  # the IN at 20 s, the wrong way
        {"kind": "possible", "direction": "in", "t": 40.4},
    ]
    s = score_camera(real, items, [{"start": 28.0, "end": 33.0}], ["in", "out"])
    i, o = s["in"], s["out"]
    assert (i["verified"], i["counted"], i["counted_real"], i["duplicates"], i["false"]) == (4, 3, 1, 1, 1)
    assert (i["check_list_real"], i["watching"], i["never_shown"]) == (1, 0, 2)
    assert i["never_shown_at"] == [20.0, 50.0]
    assert (o["counted_real"], o["wrong_direction"], o["watching"], o["never_shown"]) == (0, 1, 1, 0)
    clip = {"truth": {"independent": True},
            "cameras": {"A": {"by_direction": s, "check_items": 5, "watch_s": 5.0}}}
    tot = totals([clip], independent_only=True)["by_direction"]
    assert tot["in"]["recall_counted"] == 25.0 and tot["in"]["recall_checked"] == 50.0
    assert tot["out"]["recall_shown"] == 100.0 and tot["in"]["precision_counted"] == 33.3
    assert totals([{**clip, "truth": {"independent": False}}], independent_only=True)["clips"] == 0


def test_people_crossing_inside_a_questions_loop_can_be_counted_there() -> None:
    real = [(10.0, "out"), (11.2, "out"), (12.0, "out"), (30.0, "out")]
    items = [{"kind": "counted", "direction": "out", "t": 10.5, "clip": [8.0, 12.0]}]
    s = score_camera(real, items, [], ["out"])["out"]
    assert (s["counted_real"], s["in_loop"], s["never_shown"]) == (1, 2, 1)
    assert s["in_loop_at"] == [11.2, 12.0] and s["never_shown_at"] == [30.0]


@pytest.fixture
def recorded(two_tile_video: dict[str, Any], review_run_dir: Path, tmp_path: Path) -> Path:
    """A run folder like the wizard leaves: gate and detect outputs, detections recorded."""
    d = tmp_path / "run"
    shutil.copytree(review_run_dir, d)
    cfgs = [two_tile_video["configs"]["CAM-A"], two_tile_video["configs"]["CAM-B"]]
    for res in run_detect(two_tile_video["video"], cfgs, d, opts=DetectOptions(),
                          factory=lambda cfg, g, tile, region: RecordingDetector(
                              Oracle(two_tile_video["walkers"], tile))):
        write_detection(res, d, "derotated")
    (d / "wizard").mkdir()
    return d


def counted_by_tool(run: Path) -> list[dict[str, Any]]:
    out = []
    for sensor in ("CAM-A", "CAM-B"):
        cand = json.loads((camera_dir(run, sensor) / "candidates.json").read_text())
        out += [{"id": c["id"], "camera": sensor, "t": c["t_seconds"], "direction": c["direction"]}
                for c in cand["candidates"]]
    return out


def hand_count(run: Path, video: Path, counts: list[dict[str, Any]], watched_a: float) -> None:
    (run / "wizard" / "state.json").write_text(json.dumps({
        "mode": "manual", "direction": "both", "duration_s": 60.0, "video": str(video),
        "cameras": [{"sensor": "CAM-A", "picture": 0}, {"sensor": "CAM-B", "picture": 1}],
        "manual": {"done": True, "counts": counts,
                   "watched": {"CAM-A": [[0.0, watched_a]], "CAM-B": [[0.0, 60.0]]}}}))


def test_a_hand_counted_clip_is_scored_from_its_recorded_detections(
        two_tile_video: dict[str, Any], recorded: Path) -> None:
    proposed = counted_by_tool(recorded)
    assert proposed
    missed = {"camera": "CAM-A", "t": 5.0, "direction": "in"}  # nobody the tool saw
    hand_count(recorded, two_tile_video["video"], [*proposed, missed], watched_a=30.0)
    part = find_truth(recorded)
    assert part is not None and not part.independent
    assert part.notes == ["CAM-A: only 50% of the footage was watched"]
    hand_count(recorded, two_tile_video["video"], [*proposed, missed], watched_a=60.0)
    truth = find_truth(recorded)
    assert truth is not None and truth.kind == HAND and truth.independent

    clip = bench_clip(recorded)
    n = totals([clip], independent_only=True)["by_direction"]
    assert sum(x["verified"] for x in n.values()) == len(proposed) + 1
    assert sum(x["counted_real"] for x in n.values()) == len(proposed)  # replay = what was run
    assert sum(x["never_shown"] for x in n.values()) == 1
    assert clip["cameras"]["CAM-A"]["by_direction"]["in"]["never_shown_at"] == [5.0]


def test_a_check_of_the_tools_own_crossings_is_not_independent(
        two_tile_video: dict[str, Any], recorded: Path) -> None:
    first = next(c for c in counted_by_tool(recorded) if c["camera"] == "CAM-A")
    (recorded / "wizard" / "state.json").write_text(json.dumps({
        "mode": "auto", "direction": "both", "duration_s": 60.0,
        "video": str(two_tile_video["video"]), "job": {"status": "done"},
        "cameras": [{"sensor": "CAM-A", "picture": 0}],
        "answers": {first["id"]: "yes"}, "watched": [],
        "added": [{"camera": "CAM-A", "t": 5.0, "direction": "in"}]}))
    truth = find_truth(recorded)
    assert truth is not None and truth.kind == CHECKED and not truth.independent
    assert sorted(truth.crossings["CAM-A"]) == sorted([(first["t"], first["direction"]),
                                                       (5.0, "in")])
