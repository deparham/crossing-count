import cv2
import numpy as np
from synth import LIGHT_BLUE, texture

from crossing_count import layout as lay
from crossing_count import overlay as ov


def two_tile_frame() -> np.ndarray:
    frame = np.zeros((600, 1280, 3), dtype=np.uint8)
    frame[60:540, 0:640] = texture(480, 640, seed=1, mean=150)
    frame[60:540, 640:1280] = texture(480, 640, seed=2, mean=90)
    for x0 in (0, 640):  # translucent header bar with text, like RetailNext exports
        bar = frame[60:76, x0 : x0 + 640]
        bar[:] = (bar * 0.3).astype(np.uint8)
        cv2.putText(bar, "CAM 12:00:00.00", (4, 12), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255,) * 3, 1)
    return frame


def test_detects_side_by_side_pictures_with_headers() -> None:
    tiles = lay.detect_tiles_in(two_tile_frame())
    assert [(t.x0, t.y0, t.x1, t.y1) for t in tiles] == [(0, 60, 640, 540), (640, 60, 1280, 540)]
    assert all(14 <= t.header_px <= 18 for t in tiles)


def test_single_picture_without_header() -> None:
    tiles = lay.detect_tiles_in(texture(480, 640, seed=3))
    assert len(tiles) == 1
    assert (tiles[0].width, tiles[0].height, tiles[0].header_px) == (640, 480, 0)


def test_two_by_two_grid() -> None:
    frame = np.zeros((960, 1280, 3), dtype=np.uint8)
    for i, (x0, y0) in enumerate(((0, 0), (640, 0), (0, 480), (640, 480))):
        frame[y0 : y0 + 480, x0 : x0 + 640] = texture(480, 640, seed=10 + i, mean=70 + 40 * i)
    tiles = lay.detect_tiles_in(frame)
    assert [(t.x0, t.y0) for t in tiles] == [(0, 0), (640, 0), (0, 480), (640, 480)]


def test_empty_grid_slot_is_skipped() -> None:
    frame = np.zeros((960, 1280, 3), dtype=np.uint8)
    for i, (x0, y0) in enumerate(((0, 0), (640, 0), (0, 480))):
        frame[y0 : y0 + 480, x0 : x0 + 640] = texture(480, 640, seed=20 + i, mean=80 + 40 * i)
    assert len(lay.detect_tiles_in(frame)) == 3


def overlay_scene() -> tuple[np.ndarray, np.ndarray]:
    img = texture(480, 640, seed=5, mean=130)
    line = np.array([[80.0, 200.0], [320.0, 185.0], [560.0, 205.0]])
    cv2.polylines(img, [line.astype(np.int32)], False, LIGHT_BLUE, 1, cv2.LINE_AA)
    # Chroma subsampling, as in H.264 4:2:0, washes the thin line out.
    ycc = cv2.cvtColor(img, cv2.COLOR_BGR2YCrCb)
    for c in (1, 2):
        small = cv2.resize(ycc[..., c], (320, 240), interpolation=cv2.INTER_AREA)
        ycc[..., c] = cv2.resize(small, (640, 480), interpolation=cv2.INTER_LINEAR)
    return cv2.cvtColor(ycc, cv2.COLOR_YCrCb2BGR), line


def test_overlay_colour_is_recovered_from_a_thin_washed_out_line() -> None:
    img, line = overlay_scene()
    hsv = ov.overlay_color_from_line(img, line)
    assert hsv is not None
    assert abs(hsv[0] - 93) <= 4


def test_no_overlay_colour_without_a_burned_in_line() -> None:
    img = texture(480, 640, seed=6, mean=130)
    assert ov.overlay_color_from_line(img, np.array([[80.0, 200.0], [560.0, 205.0]])) is None


def test_line_match_score_separates_true_and_moved_lines() -> None:
    img, line = overlay_scene()
    hsv = ov.overlay_color_from_line(img, line)
    assert hsv is not None
    assert ov.line_match_score(img, line, hsv) > 0.9
    assert ov.line_match_score(img, line + [0.0, 25.0], hsv) < 0.1
