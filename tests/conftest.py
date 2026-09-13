"""Shared synthetic two-camera export, shaped like a RetailNext multi-channel download."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from synth import LIGHT_BLUE, Oracle, Walker, render_frame, texture, write_video

from crossing_count import layout as lay
from crossing_count import overlay as ov
from crossing_count import video as vid
from crossing_count.candidates import DetectOptions, run_detect, write_detection
from crossing_count.gating import GateRun, run_gate, write_outputs

FPS = 10
DURATION = 60.0
TOP = 60  # letterbox rows above the pictures
LINE_A = [(100.0, 200.0), (320.0, 190.0), (540.0, 200.0)]  # picture-local pixels
MASK_A = [(110.0, 240.0), (530.0, 240.0), (530.0, 280.0), (110.0, 280.0)]
LINE_B = [(80.0, 300.0), (560.0, 280.0)]


def _frame_walkers() -> list[Walker]:
    ax, bx = 0, 640  # picture origins (x); both at y = TOP
    return [
        # A: a clean crossing of A's line.
        Walker([(20.0, ax + 320, TOP + 100), (24.0, ax + 320, TOP + 330)]),
        # A: someone far from the line (outside the gate region) - must not open the gate.
        Walker([(8.0, ax + 60, TOP + 440), (11.0, ax + 600, TOP + 440)]),
        # A: walks into the mask zone and stands still for 18 s, then leaves.
        Walker([(35.0, ax + 200, TOP + 120), (37.0, ax + 200, TOP + 260),
                (55.0, ax + 200, TOP + 260), (58.0, ax + 200, TOP + 430)]),
        # B: standing on the line when the video starts, walks away at 8 s (ghost test).
        Walker([(0.0, bx + 300, TOP + 290), (8.0, bx + 300, TOP + 290),
                (12.0, bx + 300, TOP + 450)]),
        # B: a clean crossing of B's line.
        Walker([(30.0, bx + 420, TOP + 180), (33.0, bx + 420, TOP + 400)]),
    ]


def _draw_overlay(img: np.ndarray, t: float) -> None:
    for x0, line in ((0, LINE_A), (640, LINE_B)):
        pts = np.array([(x + x0, y + TOP) for x, y in line], dtype=np.int32)
        cv2.polylines(img, [pts], False, LIGHT_BLUE, 2, cv2.LINE_AA)
        bar = img[TOP : TOP + 16, x0 : x0 + 640]
        bar[:] = (bar * 0.3).astype(np.uint8)
        cv2.putText(bar, f"CAM 12:00:{t:05.2f}", (4, 12), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                    (255, 255, 255), 1)


def _norm(points: list[tuple[float, float]]) -> list[list[float]]:
    return [[round(x / 640, 5), round(y / 480, 5)] for x, y in points]


@pytest.fixture(scope="session")
def two_tile_video(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    d = tmp_path_factory.mktemp("synth")
    video = d / "two_cameras.mp4"
    bg = np.zeros((600, 1280, 3), dtype=np.uint8)
    bg[TOP : TOP + 480, :640] = texture(480, 640, seed=1, mean=150)
    bg[TOP : TOP + 480, 640:] = texture(480, 640, seed=2, mean=105)
    rng = np.random.default_rng(0)
    walkers = _frame_walkers()
    n = int(DURATION * FPS)
    write_video(video, (render_frame(bg, walkers, i / FPS, rng, overlay=_draw_overlay)
                        for i in range(n)), fps=FPS)

    # Record each camera's overlay colour the way trace_line.py would.
    med = lay.median_image([f.image for f in vid.grab_frames_at(video, [5, 15, 25, 45, 50])])
    tiles = lay.detect_tiles_in(med)
    configs = {}
    for name, tile, line, extra in (
        ("CAM-A", tiles[0], LINE_A, {"mask_zone": _norm(MASK_A)}),
        ("CAM-B", tiles[1], LINE_B, {}),
    ):
        hsv = ov.overlay_color_from_line(tile.crop(med), np.asarray(line))
        assert hsv is not None
        raw = {"site": "SYN", "sensor": name, "line": _norm(line), "inside_side": "below",
               "fps_assumed": FPS, "overlay_hsv": list(hsv),
               "traced_on": {"tile_size": [640, 480]}, **extra}
        p = d / f"{name}.json"
        p.write_text(json.dumps(raw))
        configs[name] = p
    return {"video": video, "configs": configs, "dir": d, "walkers": walkers}


@pytest.fixture(scope="session")
def gate_run(two_tile_video: dict[str, Any]) -> GateRun:
    cfgs = two_tile_video["configs"]
    return run_gate(two_tile_video["video"], [cfgs["CAM-B"], cfgs["CAM-A"]])


@pytest.fixture(scope="session")
def review_run_dir(two_tile_video: dict[str, Any],
                   tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A run directory with gate and detect outputs (oracle detector), for review tests."""
    d = tmp_path_factory.mktemp("review_run")
    cfgs = [two_tile_video["configs"]["CAM-A"], two_tile_video["configs"]["CAM-B"]]
    write_outputs(run_gate(two_tile_video["video"], cfgs), d)
    for res in run_detect(
            two_tile_video["video"], cfgs, d, opts=DetectOptions(),
            factory=lambda cfg, g, tile, region: Oracle(two_tile_video["walkers"], tile)):
        write_detection(res, d, "derotated")
    return d


@pytest.fixture
def fresh(review_run_dir: Path) -> Path:
    """The review run directory with any saved review decisions removed."""
    state = review_run_dir / "review" / "decisions.json"
    if state.exists():
        state.unlink()
    return review_run_dir
