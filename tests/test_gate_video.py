"""End-to-end gate behaviour on a synthetic two-camera export."""

from typing import Any

import pytest

from crossing_count.config import ConfigError
from crossing_count.gating import GateRun, camera_result, in_ranges, run_gate


def result(run: GateRun, sensor: str) -> dict[str, Any]:
    cam = next(c for c in run.cameras if c.cfg.sensor == sensor)
    return camera_result(run, cam)


def spans(res: dict[str, Any]) -> list[tuple[float, float]]:
    return [(r["start_s"], r["end_s"]) for r in res["ranges"]]


def covered(res: dict[str, Any], start: float, end: float) -> bool:
    return any(s <= start and end <= e for s, e in spans(res))


def test_cameras_are_matched_to_their_pictures_by_overlay(gate_run: GateRun) -> None:
    placed = {c.cfg.sensor: c.placement for c in gate_run.cameras}
    assert placed["CAM-A"].tile.index == 0 and placed["CAM-B"].tile.index == 1
    for p in placed.values():
        own = p.scores[p.tile.index]
        other = p.scores[1 - p.tile.index]
        assert own is not None and own > 0.9
        assert other is not None and other < 0.3
        assert not p.warnings


def test_a_crossing_is_kept(gate_run: GateRun) -> None:
    assert covered(result(gate_run, "CAM-A"), 20.0, 24.0)
    assert covered(result(gate_run, "CAM-B"), 30.0, 33.0)


def test_motion_far_from_the_line_does_not_open_the_gate(gate_run: GateRun) -> None:
    res = result(gate_run, "CAM-A")
    assert not any(in_ranges(t, spans(res)) for t in (8.0, 9.5, 11.0))


def test_a_person_standing_still_in_the_mask_zone_keeps_the_gate_open(gate_run: GateRun) -> None:
    assert covered(result(gate_run, "CAM-A"), 35.0, 57.0)


def test_someone_present_at_the_start_leaves_no_ghost(gate_run: GateRun) -> None:
    # CAM-B's person stands on the line until 8 s and is gone by 12 s. With a
    # background learned from the first frames, their outline would stay
    # "foreground" for minutes; the median bootstrap must prevent that.
    res = result(gate_run, "CAM-B")
    assert covered(res, 0.0, 10.0)
    assert not any(in_ranges(t, spans(res)) for t in (18.0, 22.0, 26.0, 40.0, 50.0))


def test_pictures_do_not_leak_into_each_other(gate_run: GateRun) -> None:
    # CAM-A's crossing (20-24 s) happens only in picture 0.
    assert not in_ranges(22.0, spans(result(gate_run, "CAM-B")))


def test_output_is_deterministic(two_tile_video: dict[str, Any], gate_run: GateRun) -> None:
    cfgs = two_tile_video["configs"]
    again = run_gate(two_tile_video["video"], [cfgs["CAM-B"], cfgs["CAM-A"]])
    for sensor in ("CAM-A", "CAM-B"):
        assert result(again, sensor) == result(gate_run, sensor)


def test_sample_limits_processing(two_tile_video: dict[str, Any]) -> None:
    cfgs = two_tile_video["configs"]
    run = run_gate(two_tile_video["video"], [cfgs["CAM-A"]], sample_s=15.0)
    res = result(run, "CAM-A")
    assert res["processed_duration_s"] == 15.0
    assert all(e <= 15.0 for _, e in spans(res))


def test_multi_picture_video_needs_overlay_or_explicit_tile(
    two_tile_video: dict[str, Any], tmp_path: Any
) -> None:
    import json

    raw = json.loads(two_tile_video["configs"]["CAM-B"].read_text())
    raw.pop("overlay_hsv")
    p = tmp_path / "no_overlay.json"
    p.write_text(json.dumps(raw))
    with pytest.raises(ConfigError, match="--tile"):
        run_gate(two_tile_video["video"], [p], sample_s=5.0)
    run = run_gate(two_tile_video["video"], [p], sample_s=5.0, tile_map={"CAM-B": 1})
    assert run.cameras[0].placement.tile.index == 1


def test_a_clean_video_uses_the_picture_the_config_was_drawn_on(
    two_tile_video: dict[str, Any], tmp_path: Any
) -> None:
    import json

    raw = json.loads(two_tile_video["configs"]["CAM-B"].read_text())
    raw["traced_on"]["tile_index"] = 1
    raw["overlay_hsv"] = [0, 255, 255]  # a colour nowhere in the video: no burned-in line
    marked = tmp_path / "drawn_on_marked_export.json"
    marked.write_text(json.dumps(raw))
    raw.pop("overlay_hsv")
    clean = tmp_path / "drawn_on_clean_video.json"
    clean.write_text(json.dumps(raw))
    for p in (marked, clean):
        (cam,) = run_gate(two_tile_video["video"], [p], sample_s=5.0).cameras
        assert cam.placement.tile.index == 1
        assert any("drawn on" in w for w in cam.placement.warnings)
