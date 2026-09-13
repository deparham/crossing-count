import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from crossing_count import tracing
from crossing_count import video as vid
from crossing_count.config import ConfigError
from crossing_count.gating import build_ranges, mog2_learning_rate


def ranges(active_times: list[float], duration: float = 60.0, dt: float = 0.1,
           pre: float = 3.0, post: float = 3.0, gap: float = 5.0) -> list[tuple[float, float]]:
    times = np.round(np.arange(0.0, duration, dt), 3)
    act = np.isin(times, np.round(active_times, 3))
    return [(round(s, 3), round(e, 3)) for s, e in build_ranges(times, act, dt, pre, post, gap,
                                                                  duration)]


def test_no_activity_gives_no_ranges() -> None:
    assert ranges([]) == []


def test_single_frame_is_padded_both_sides() -> None:
    assert ranges([20.0]) == [(16.95, 23.05)]


def test_ranges_clamp_to_the_video() -> None:
    assert ranges([0.0, 59.9]) == [(0.0, 3.05), (56.85, 60.0)]


def test_nearby_ranges_merge_and_distant_ones_do_not() -> None:
    assert ranges([10.0, 18.0]) == [(6.95, 21.05)]  # padded gap 1.9 s < 5 s
    assert ranges([10.0, 30.0]) == [(6.95, 13.05), (26.95, 33.05)]


def test_learning_rate_absorbs_after_about_absorb_s() -> None:
    a = mog2_learning_rate(60.0, 10.0)
    n = np.log(0.9) / np.log(1 - a)
    assert n == pytest.approx(600, rel=1e-6)


# --- tracing helpers ----------------------------------------------------------------------

def build(**kw: Any) -> Any:
    args: dict[str, Any] = dict(
        site="S", sensor="CAM", tile_size=(640, 480),
        line=[(100.0, 200.0), (320.0, 190.0), (540.0, 200.0)], inside_point=(320.0, 260.0),
        mask_zone=None, filter_zones=[], exclusion_zones=[], gate_ignore_zones=[], fps=10.0,
        overlay_hsv=(93, 150, 180), traced_on={"tile_size": [640, 480]},
    )
    args.update(kw)
    return tracing.build_config_dict(**args)


def test_trace_infers_inside_side_from_the_click() -> None:
    raw, _ = build()
    assert raw["inside_side"] == "below"
    raw, _ = build(inside_point=(320.0, 120.0))
    assert raw["inside_side"] == "above"
    raw, _ = build(line=[(300.0, 50.0), (310.0, 430.0)], inside_point=(400.0, 240.0))
    assert raw["inside_side"] in ("left", "right")


def test_trace_rejects_mask_on_the_wrong_side() -> None:
    with pytest.raises(ConfigError, match="inside"):
        build(mask_zone=[(120.0, 100.0), (520.0, 100.0), (520.0, 140.0), (120.0, 140.0)])


def test_trace_rejects_click_on_the_line() -> None:
    with pytest.raises(ConfigError, match="on the line"):
        build(inside_point=(100.0, 200.0))


def test_trace_drops_double_clicks() -> None:
    raw, _ = build(line=[(100.0, 200.0), (100.2, 200.1), (540.0, 200.0)])
    assert len(raw["line"]) == 2


# --- video timing -------------------------------------------------------------------------

def test_parse_retailnext_filename_interval() -> None:
    fi = vid.parse_filename_interval(
        "Export - Multiple Channels - 2026-09-12-125627 AEST to 2026-09-12-130127 AEST.mp4")
    assert fi is not None
    assert (fi.start, fi.tz, fi.seconds) == ("2026-09-12T12:56:27", "AEST", 300.0)
    assert vid.parse_filename_interval("clip.mp4") is None


def test_fps_mismatch_warning() -> None:
    assert vid.fps_mismatch_warning(10.0, 25.0) is not None
    assert vid.fps_mismatch_warning(10.0, 10.0) is None


def test_audit_flags_frames_missing_against_filename(two_tile_video: dict[str, Any],
                                                     tmp_path: Path) -> None:
    src: Path = two_tile_video["video"]
    ok = tmp_path / "Export - X - 2026-01-01-120000 AEST to 2026-01-01-120100 AEST.mp4"
    short = tmp_path / "Export - X - 2026-01-01-120000 AEST to 2026-01-01-120200 AEST.mp4"
    shutil.copy(src, ok)
    shutil.copy(src, short)
    a_ok = vid.audit_timebase(ok)
    assert a_ok.effective_fps == pytest.approx(10.0)
    assert not any("filename" in w for w in a_ok.warnings)
    assert any("dropped" in w for w in vid.audit_timebase(short).warnings)
