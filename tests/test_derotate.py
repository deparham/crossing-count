import math

import numpy as np
import pytest

from crossing_count import derotate as dr

CENTER = np.array([320.0, 240.0])


@pytest.mark.parametrize("anchor", [(320, 400), (500, 240), (320, 60), (100, 100), (600, 450)])
def test_outward_radius_points_up_in_the_crop(anchor: tuple[float, float]) -> None:
    alpha = dr.upright_angle(anchor, CENTER, min_radius=20)
    m, _ = dr._affine(anchor, alpha, 200)
    outward = np.array(anchor) + 30 * (np.array(anchor) - CENTER) / np.hypot(*(np.array(anchor) - CENTER))
    a = m[:, :2] @ np.array(anchor) + m[:, 2]
    o = m[:, :2] @ outward + m[:, 2]
    assert a == pytest.approx([100, 100])  # the anchor is the crop centre
    assert o[0] == pytest.approx(100, abs=1e-6) and o[1] < 100  # outward is straight up


def test_no_rotation_under_the_lens() -> None:
    assert dr.upright_angle((325.0, 245.0), CENTER, min_radius=20) == 0.0


def test_crop_to_frame_round_trip() -> None:
    region = np.zeros((480, 640), dtype=np.uint8)
    region[250:400, 100:500] = 255
    specs = dr.plan_crops(region, CENTER, crop_px=200, step_px=100, min_radius_px=20)
    assert specs
    rng = np.random.default_rng(0)
    for spec in specs:
        pts = rng.uniform(0, 200, (5, 2))
        frame_pts = dr.to_frame(spec, pts)
        back = frame_pts @ spec.m[:, :2].T + spec.m[:, 2]
        assert back == pytest.approx(pts)


def test_crops_cover_the_region() -> None:
    region = np.zeros((480, 640), dtype=np.uint8)
    region[250:400, 100:500] = 255
    specs = dr.plan_crops(region, CENTER, crop_px=200, step_px=100, min_radius_px=20)
    covered = np.zeros_like(region)
    for s in specs:
        ys, xs = np.mgrid[0:480, 0:640]
        d = np.hypot(xs - s.anchor[0], ys - s.anchor[1])
        covered[d <= 100] = 255  # inscribed circle of the rotated crop
    assert covered[region > 0].all()


def test_warp_puts_a_radial_bar_upright() -> None:
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    # A "person" lying along the radius to the right of centre: a horizontal bar.
    frame[235:245, 480:540] = 255
    anchor = (510.0, 240.0)
    spec = dr.CropSpec(0, anchor, dr.upright_angle(anchor, CENTER, 20), 120,
                       *dr._affine(anchor, dr.upright_angle(anchor, CENTER, 20), 120))
    crop = dr.warp(frame, spec)
    ys, xs = np.nonzero(crop[..., 0] > 128)
    assert (ys.max() - ys.min()) > 3 * (xs.max() - xs.min())  # now vertical


def test_radial_foot_point_is_on_the_lens_side() -> None:
    foot = dr.radial_foot_point((480, 200, 520, 280), CENTER)  # box right of centre
    assert foot[0] == pytest.approx(480) and foot[1] == pytest.approx(240)
    assert dr.radial_foot_point((300, 220, 340, 260), CENTER) == pytest.approx([320, 240])
    assert math.isfinite(float(dr.radial_foot_point((0, 0, 10, 10), CENTER)[0]))
