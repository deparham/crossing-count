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


def test_a_hard_edge_inside_one_picture_is_not_a_join_between_two() -> None:
    """The bug this guards: a fisheye entrance (Martin Place, 640x480, one camera) has a
    pillar down the middle of the lower third. Averaged down the column that edge scored
    22.5 against a threshold of 22.1, so the picture was split and counted as two cameras -
    one of them the half with no camera name in its header."""
    frame = texture(480, 640, seed=7, mean=120)
    frame[300:480, 310:316] = 250  # a bright pillar, over the lower 38% of the picture only
    tiles = lay.detect_tiles_in(frame)
    assert len(tiles) == 1, [(t.x0, t.x1) for t in tiles]
    assert (tiles[0].width, tiles[0].height) == (640, 480)


def test_the_whole_header_bar_is_found_not_just_its_darkest_rows() -> None:
    """Two bugs this guards, both leaving a clock that changes every frame inside the tracked
    picture. Martin Place's bar measures 70.5 where the cut-off was 70, and being too bright
    stopped the search rather than ruling out that one answer. 392's bar runs 40 at its top
    to 74 through its body, and that rise inside the bar read as the picture starting, so 4
    of its 16 rows were called the header. Every real export's bar is 16 rows."""
    barely = texture(480, 640, seed=8, mean=210)
    barely[:16] = 70  # only just darker than the cut-off allowed
    assert lay.detect_tiles_in(barely)[0].header_px == 16

    graded = texture(480, 640, seed=8, mean=210)
    graded[:4] = 42  # a bar with its own top rows darker than the rest of it
    graded[4:14] = 74
    graded[14:16] = 62
    assert lay.detect_tiles_in(graded)[0].header_px == 16

    plain = texture(480, 640, seed=9, mean=120)  # no bar: the picture starts at the top row
    assert lay.detect_tiles_in(plain)[0].header_px == 0


def test_a_hard_edge_as_strong_as_a_real_join_is_still_not_one() -> None:
    """The bug this guards: CN-407's left camera has an edge down its whole height that is
    as sharp as the join between the two cameras (median difference 68 against the join's
    53), so nothing about the seam itself tells them apart. The layout does: two cameras in
    a 1280-wide export make two 640s, and 302, 338, 640 is not a grid."""
    frame = np.zeros((960, 1280, 3), dtype=np.uint8)
    frame[240:720, 0:640] = texture(480, 640, seed=11, mean=120)
    frame[240:720, 640:1280] = texture(480, 640, seed=12, mean=60)
    frame[240:720, 296:302] = 240  # a wall down the whole of the left camera's picture
    tiles = lay.detect_tiles_in(frame)
    assert [(t.x0, t.x1) for t in tiles] == [(0, 640), (640, 1280)], [(t.x0, t.x1) for t in tiles]
    assert len({t.width for t in tiles}) == 1  # the pictures of one export are the same size


def test_the_grid_keeps_the_most_pictures_the_seams_agree_with() -> None:
    assert lay._grid([], 640) == []  # one picture
    assert lay._grid([640], 1280) == [640]  # two
    assert lay._grid([640, 1280], 1920) == [640, 1280]  # three in a row
    assert lay._grid([320, 640, 960], 1280) == [320, 640, 960]  # four
    assert lay._grid([302, 640], 1280) == [640]  # a wall inside the left picture, and the join
    assert lay._grid([302], 1280) == []  # the wall alone is not a join
    assert lay._grid([636], 1280) == [636]  # a join a few pixels off centre is still the join
    assert lay._grid([320, 960], 1280) == []  # two of the three a four-way grid needs


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


def test_a_line_across_the_middle_does_not_halve_one_picture() -> None:
    """The bug this guards: TC-410 is one 640x480 camera with a hard line straight across
    the middle of its picture. The seam holds the whole way and 240 halves 480 evenly, so
    neither the seam nor the grid can refuse it; it came out as two cameras, the lower one
    with no name in its header. What gives it away is the shape: 640x239 is not a picture."""
    frame = texture(480, 640, seed=13, mean=110)
    frame[236:244] = 250  # a hard line straight across, at the exact halfway point
    tiles = lay.detect_tiles_in(frame)
    assert len(tiles) == 1, [(t.y0, t.y1) for t in tiles]
    assert (tiles[0].width, tiles[0].height) == (640, 480)


def test_what_counts_as_the_shape_of_a_picture() -> None:
    assert lay._shaped_like_pictures([0, 640], [0, 480])  # one 4:3 picture
    assert lay._shaped_like_pictures([0, 640, 1280], [0, 480])  # two side by side
    assert lay._shaped_like_pictures([0, 640, 1280], [0, 480, 960])  # 2x2
    assert not lay._shaped_like_pictures([0, 640], [0, 240, 480])  # halved across: 640x240
    assert not lay._shaped_like_pictures([0, 160, 320, 480, 640], [0, 480])  # quartered down


# ---------- when the export says how many cameras it holds ----------
# A RetailNext download is asked for named channels (CN-212-PB1, CN-212-PB2, ...) and the
# names are kept beside the file. Then the number of pictures is a fact, not something to
# work out from the pixels, and the two ways the pixels get it wrong both stop happening.

def _joined(parts: list[np.ndarray], across: bool = True) -> np.ndarray:
    return np.concatenate(parts, axis=1 if across else 0)


def test_two_cameras_that_look_like_one_are_still_split_when_the_count_is_known() -> None:
    """The bug: two channels whose pictures meet with almost no step between them - similar
    scenes, similar brightness - read as one camera, so one line and one row in the report
    stood for two entrances."""
    left = texture(480, 640, seed=11, mean=120)
    right = texture(480, 640, seed=12, mean=121)  # all but identical: no seam to find
    frame = _joined([left, right])
    assert len(lay.detect_tiles_in(frame)) == 1  # what the pixels alone make of it

    tiles = lay.detect_tiles_in(frame, expect=2)
    assert [(t.x0, t.x1) for t in tiles] == [(0, 640), (640, 1280)]
    assert all(t.height == 480 for t in tiles)


def test_one_camera_with_a_hard_line_is_not_split_when_the_count_is_known() -> None:
    """The other way round: a single fisheye with a pillar or a counter line down it. Told
    there is one camera, nothing can halve it."""
    frame = texture(480, 640, seed=13, mean=120)
    frame[:, 318:322] = 250  # a hard edge down the whole height, on an even division
    tiles = lay.detect_tiles_in(frame, expect=1)
    assert len(tiles) == 1
    assert (tiles[0].width, tiles[0].height) == (640, 480)


def test_the_arrangement_the_picture_agrees_with_wins() -> None:
    """Four cameras can be a row or a square, and both give four. The joins decide."""
    square = _joined([_joined([texture(240, 320, seed=s, mean=m) for s, m in pair])
                      for pair in (((21, 90), (22, 170)), ((23, 60), (24, 200)))], across=False)
    assert square.shape == (480, 640, 3)
    tiles = lay.detect_tiles_in(square, expect=4)
    assert len(tiles) == 4
    assert [(t.x0, t.y0) for t in tiles] == [(0, 0), (320, 0), (0, 240), (320, 240)]

    row = _joined([texture(480, 320, seed=s, mean=m)
                   for s, m in ((31, 80), (32, 160), (33, 70), (34, 190))])
    inline = lay.detect_tiles_in(row, expect=4)
    assert [(t.x0, t.x1) for t in inline] == [(0, 320), (320, 640), (640, 960), (960, 1280)]


def test_three_cameras_side_by_side() -> None:
    row = _joined([texture(480, 400, seed=s, mean=m) for s, m in ((41, 80), (42, 170), (43, 60))])
    tiles = lay.detect_tiles_in(row, expect=3)
    assert [(t.x0, t.x1) for t in tiles] == [(0, 400), (400, 800), (800, 1200)]


def test_five_cameras_in_a_grid_with_a_slot_to_spare() -> None:
    """CN-323 and YD-407 have five cameras each. Four was the most anything here could split
    into, so their exports came out as four pictures or one."""
    row = [texture(240, 320, seed=s, mean=m) for s, m in ((51, 80), (52, 170), (53, 60))]
    second = [texture(240, 320, seed=s, mean=m) for s, m in ((54, 190), (55, 100))]
    second.append(np.zeros((240, 320, 3), dtype=np.uint8))  # the sixth slot, empty
    frame = _joined([_joined(row), _joined(second)], across=False)
    assert frame.shape == (480, 960, 3)

    tiles = lay.detect_tiles_in(frame, expect=5)
    assert len(tiles) == 5, [(t.x0, t.y0, t.x1, t.y1) for t in tiles]
    assert [(t.x0, t.y0) for t in tiles] == [(0, 0), (320, 0), (640, 0), (0, 240), (320, 240)]


def test_reading_the_pixels_alone_still_refuses_to_find_five() -> None:
    """Being told five is not a guess; guessing five out of the pixels is still refused, which
    is what keeps one camera's picture from being cut into pieces."""
    row = _joined([texture(480, 256, seed=s, mean=m)
                   for s, m in ((61, 60), (62, 180), (63, 70), (64, 200), (65, 90))])
    assert len(lay.detect_tiles_in(row)) <= lay.GRID_MAX


def test_a_count_that_cannot_be_made_falls_back_to_reading_the_pixels() -> None:
    """Never worse than before: if nothing divides into that many, the old way still answers."""
    tiles = lay.detect_tiles_in(two_tile_frame(), expect=99)
    assert [(t.x0, t.x1) for t in tiles] == [(0, 640), (640, 1280)]
    assert lay.detect_tiles_in(two_tile_frame(), expect=0) == lay.detect_tiles_in(two_tile_frame())
