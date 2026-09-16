"""Detectors are swappable, every recorded detection says which one made it, a score never
mixes two, and the benchmark says what the checking costs and what never moved."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from crossing_count import bench, gold
from crossing_count.candidates import (
    DetectOptions,
    backbone_factory,
    output_dir,
    replay_factory,
    run_detect,
    write_detection,
)
from crossing_count.detector import Box
from crossing_count.tracks import EOF, Track, TrackSample


class FakeBackbone:
    """A detector that always finds one person in the same place: the pipeline around it is
    what is under test."""

    kind = "fake"

    def __init__(self) -> None:
        self.asked: list[tuple[int, int]] = []

    def boxes(self, images: Any, conf: float, imgsz: int) -> list[list[tuple[Box, float]]]:
        self.asked.append((len(images), imgsz))
        return [[((20.0, 20.0, 50.0, 90.0), 0.9)] for _ in images]

    def about(self) -> dict[str, Any]:
        return {"backbone": self.kind, "model": "fake-1", "weights": "fake.pt",
                "weights_sha256": "f" * 64, "device": "cpu"}


@pytest.fixture
def run_dir(review_run_dir: Path, tmp_path: Path) -> Path:
    d = tmp_path / "run"
    shutil.copytree(review_run_dir, d)
    return d


def test_the_crop_pipeline_is_the_same_whichever_detector_runs(
        two_tile_video: dict[str, Any], run_dir: Path) -> None:
    cfgs = [two_tile_video["configs"]["CAM-A"]]
    sizes = {}
    for mode in ("derotated", "naive"):
        box = FakeBackbone()
        opts = DetectOptions(mode=mode, backbone="fake")
        res = run_detect(two_tile_video["video"], cfgs, run_dir, opts=opts,
                         factory=backbone_factory(opts, box))
        assert res and box.asked  # the same backbone served both pipelines
        sizes[mode] = {s for _, s in box.asked}
        det = res[0].candidates["detector"]
        assert (det["backbone"], det["model"]) == ("fake", "fake-1")
        assert det["mode"] == mode and det["weights_sha256"] == "f" * 64
    # de-rotation asks for small upright crops, the naive baseline for the whole picture:
    # both outside the detector, which only ever sees pictures
    assert sizes["derotated"] == {320} and sizes["naive"] == {640}


def test_a_recorded_set_says_which_detector_made_it(
        two_tile_video: dict[str, Any], run_dir: Path) -> None:
    cfgs = [two_tile_video["configs"]["CAM-A"]]
    opts = DetectOptions(mode="naive", backbone="fake", record=True)
    (res,) = run_detect(two_tile_video["video"], cfgs, run_dir, opts=opts,
                        factory=backbone_factory(opts, FakeBackbone()))
    write_detection(res, run_dir, "fake-naive")
    kept = output_dir(run_dir, "CAM-A", "fake-naive")
    assert (kept / "detections.pkl").is_file()
    assert json.loads((kept / "candidates.json").read_text())["detector"]["backbone"] == "fake"
    assert bench.variants(run_dir) == ["fake-naive"]
    assert bench.detector_of(run_dir, "fake-naive")["weights_sha256"] == "f" * 64
    # replaying that set keeps saying which detector produced it
    again = run_detect(two_tile_video["video"], cfgs, run_dir, opts=DetectOptions(),
                       factory=replay_factory(run_dir, "fake-naive"))
    assert again[0].candidates["detector"]["backbone"] == "fake"
    assert again[0].candidates["candidates"] == res.candidates["candidates"]


def test_a_score_may_not_mix_two_detectors(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    clips = [{"id": f"c{k}", "split": "train", "store": {"code": "S1"}, "tags": [],
              "reviews": [{"reviewer": "Alex", "footage": {"clean": True,
                                                           "checked_in_picture": True}}],
              "conditions": {}} for k in (1, 2)]
    monkeypatch.setattr(gold, "clips", lambda root=None, shared=None: clips)

    def scored(rec: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
        which = "yolo11s.pt" if rec["id"] == "c1" else "yolo11m.pt"
        return {"id": rec["id"], "split": "train", "tags": [], "groups": [], "store": "S1",
                "scored": True, "uncertain": 0, "agreement": None, "camera_hours": 0.25,
                "by_direction": {}, "questions": 0, "listed": 0, "missed": 0,
                "misses_on_list": 0, "watch_s": 0.0,
                "detectors": [{"model": which, "backbone": "yolo", "sha256": which}]}

    monkeypatch.setattr(gold, "score_clip", scored)
    with pytest.raises(gold.GoldError, match="different detectors"):
        gold.evaluate("development", tmp_path)


def _track(tid: int, xs: list[tuple[float, float, float]]) -> Track:
    return Track(tid, [TrackSample(t, x, y, (x, y, x + 10, y + 20), 0.9) for t, x, y in xs], EOF)


def test_things_that_never_move_are_their_own_kind_of_false_positive() -> None:
    class Res:
        candidates = {"picture": {"tile": {"y0": 0, "y1": 480}}}
        tracks = [_track(1, [(0.0, 100.0, 100.0), (9.0, 100.4, 100.2)]),  # a mannequin
                  _track(2, [(0.0, 10.0, 10.0), (9.0, 300.0, 300.0)]),  # a person walking
                  _track(3, [(0.0, 50.0, 50.0), (1.0, 50.0, 50.0)])]  # still, but only 1 s

    items = [{"track_id": 1}, {"track_id": 2}, {"tracks": [1, 5]}]
    s = bench.static_objects(Res(), items)  # type: ignore[arg-type]
    assert (s["tracks"], s["items"]) == (1, 2) and s["seconds"] == 9.0


def test_the_benchmark_says_what_the_checking_costs(tmp_path: Path) -> None:
    (tmp_path / "wizard").mkdir()
    # five answers a minute apart, then a long break: the break is not reviewing time
    at = [f"2026-09-15T10:{m:02d}:00+10:00" for m in (0, 1, 2, 3, 4)] + ["2026-09-15T11:30:00+10:00"]
    (tmp_path / "wizard" / "state.json").write_text(json.dumps(
        {"decisions": [{"action": "answer", "at": t} for t in at]}))
    minutes, how = bench.review_minutes(tmp_path, items=40)
    assert (minutes, how) == (4.0, "measured from the answers' times")
    quiet = tmp_path / "other"
    (quiet / "wizard").mkdir(parents=True)
    assert bench.review_minutes(quiet, items=40)[0] == round(40 * bench.SECONDS_PER_ITEM / 60, 1)
    clip = {"truth": {"independent": True}, "review_minutes": 30.0,
            "cameras": {"A": {"by_direction": {}, "check_items": 190, "watch_s": 720.0,
                              "footage_s": 900.0, "static": {"tracks": 2, "items": 6}},
                        "B": {"by_direction": {}, "check_items": 0, "watch_s": 0.0,
                              "footage_s": 900.0, "static": {"tracks": 0, "items": 0}}}}
    tot = bench.totals([clip], independent_only=True)
    assert tot["camera_hours"] == 0.5 and tot["items_per_camera_hour"] == 380.0
    assert tot["review_minutes_per_camera_hour"] == 60.0 and tot["watch_share_pct"] == 40.0
    assert tot["static_items"] == 6 and tot["static_items_per_camera_hour"] == 12.0
