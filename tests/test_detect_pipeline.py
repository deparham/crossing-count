"""M2 end to end on the synthetic two-camera export, with an oracle detector.

The oracle returns each synthetic person's true position, so these tests
exercise gating, tracking (real ByteTrack), stitching, the rule and the output
files without depending on YOLO.
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path
from typing import Any

import pytest
from synth import Oracle

from crossing_count.candidates import DetectOptions, run_detect, write_detection
from crossing_count.config import ConfigError
from crossing_count.gating import run_gate, write_outputs
from crossing_count.layout import Tile


@pytest.fixture(scope="module")
def pipeline(two_tile_video: dict[str, Any], tmp_path_factory: pytest.TempPathFactory) -> Any:
    run_dir = tmp_path_factory.mktemp("run")
    cfgs = [two_tile_video["configs"]["CAM-A"], two_tile_video["configs"]["CAM-B"]]
    write_outputs(run_gate(two_tile_video["video"], cfgs), run_dir)

    def factory(cfg: Any, geom: Any, tile: Tile, region: Any) -> Oracle:
        return Oracle(two_tile_video["walkers"], tile)

    def run() -> dict[str, Any]:
        res = run_detect(two_tile_video["video"], cfgs, run_dir, opts=DetectOptions(),
                         factory=factory)
        return {r.cfg.sensor: r for r in res}

    return run(), run, run_dir


def times(res: Any) -> list[tuple[str, float]]:
    return [(c["direction"], c["t_seconds"]) for c in res.candidates["candidates"]]


def test_mask_camera_counts_both_entries_at_the_line(pipeline: Any) -> None:
    res = pipeline[0]["CAM-A"]
    got = times(res)
    assert [d for d, _ in got] == ["in", "in"]
    assert got[0][1] == pytest.approx(21.57, abs=0.15)  # crosses exactly at a vertex
    assert got[1][1] == pytest.approx(36.0, abs=0.15)
    first, second = res.candidates["candidates"]
    assert first["line_position"] == pytest.approx(0.5, abs=0.02)
    assert all(c["dwell_s"] >= 0.4 for c in (first, second))
    assert first["clip_start"] <= first["t_seconds"] <= first["clip_end"]
    assert first["path"] and second["concurrent_tracks"] >= 1
    assert res.discarded["discarded"] == []
    assert not res.candidates["summary"]["pending_expired_alarm"]


def test_line_only_camera_counts_its_crossings(pipeline: Any) -> None:
    res = pipeline[0]["CAM-B"]
    got = times(res)
    assert [d for d, _ in got] == ["in", "in"]
    assert got[0][1] == pytest.approx(8.02, abs=0.15)
    assert got[1][1] == pytest.approx(31.44, abs=0.15)
    assert all(c["dwell_s"] is None for c in res.candidates["candidates"])


def test_outputs_are_written_and_deterministic(pipeline: Any, tmp_path: Path) -> None:
    first, run, run_dir = pipeline
    again = run()
    for sensor in ("CAM-A", "CAM-B"):
        assert again[sensor].candidates == first[sensor].candidates
        assert again[sensor].discarded == first[sensor].discarded
        assert again[sensor].unexplained == first[sensor].unexplained
    out = write_detection(first["CAM-A"], run_dir, "derotated")
    assert {p.name for p in out.iterdir()} >= {"candidates.json", "discarded.json",
                                               "unexplained.json", "activity.json"}


def test_summary_gives_per_camera_and_total_counts_with_clock_times(pipeline: Any) -> None:
    from datetime import datetime

    from crossing_count.summary import format_summary, summarise

    results, _, run_dir = pipeline
    for res in results.values():
        write_detection(res, run_dir, "derotated")
    s = summarise(run_dir, datetime(2026, 9, 12, 11, 30, 0, tzinfo=UTC))
    assert [c["sensor"] for c in s["cameras"]] == ["CAM-A", "CAM-B"]
    assert s["total"]["in"] == 4 and s["total"]["out"] == 0
    first = s["cameras"][0]["crossings"][0]
    assert first["clock"] == "11:30:21"  # 21.57 s into the video
    text = "\n".join(format_summary(s, "synthetic.mp4", None))
    assert "TOTAL proposed: IN 4, OUT 0" in text and "not yet reviewed" in text


def test_recorded_detections_replay_to_the_same_result(
    two_tile_video: dict[str, Any], tmp_path: Path
) -> None:
    from crossing_count.candidates import RecordingDetector, replay_factory

    cfgs = [two_tile_video["configs"]["CAM-A"], two_tile_video["configs"]["CAM-B"]]
    write_outputs(run_gate(two_tile_video["video"], cfgs), tmp_path)
    live = run_detect(
        two_tile_video["video"], cfgs, tmp_path, opts=DetectOptions(),
        factory=lambda cfg, g, tile, region: RecordingDetector(
            Oracle(two_tile_video["walkers"], tile)))
    for res in live:
        assert res.recorded is not None and res.recorded["frames"]
        write_detection(res, tmp_path, "derotated")
    replayed = run_detect(two_tile_video["video"], cfgs, tmp_path, opts=DetectOptions(),
                          factory=replay_factory(tmp_path))
    for a, b in zip(live, replayed):
        assert b.candidates["candidates"] == a.candidates["candidates"]
        assert b.discarded["discarded"] == a.discarded["discarded"]


def test_detect_refuses_a_mismatched_sample(pipeline: Any, two_tile_video: dict[str, Any]) -> None:
    _, _, run_dir = pipeline
    with pytest.raises(ConfigError, match="sample"):
        run_detect(two_tile_video["video"], [two_tile_video["configs"]["CAM-A"]], run_dir,
                   opts=DetectOptions(), sample_s=10.0,
                   factory=lambda *a: Oracle(two_tile_video["walkers"], a[2]))
