"""Telling footage with the sensor's burned-in lines from clean footage."""

from __future__ import annotations

import cv2
import numpy as np
from synth import LIGHT_BLUE, texture

from crossing_count.overlay import counting_overlay_evidence


def test_burned_in_lines_are_found() -> None:
    img = texture(480, 640, seed=3)
    for y in (150, 260):  # a counting line and a zone edge, as RetailNext draws them
        pts = np.array([[60, y], [320, y - 12], [580, y]], np.int32)
        cv2.polylines(img, [pts], False, LIGHT_BLUE, 2, cv2.LINE_AA)
    cv2.polylines(img, [np.array([[80, 330], [560, 330], [560, 460], [80, 460]], np.int32)],
                  True, LIGHT_BLUE, 2, cv2.LINE_AA)
    ev = counting_overlay_evidence(img)
    assert ev["marks"], ev


def test_clean_pictures_and_blue_signs_are_not_marks() -> None:
    img = texture(480, 640, seed=4)
    assert not counting_overlay_evidence(img)["marks"]
    cv2.rectangle(img, (200, 150), (400, 300), LIGHT_BLUE, -1)  # a big blue sign
    cv2.circle(img, (520, 380), 50, LIGHT_BLUE, -1)  # a blue display
    ev = counting_overlay_evidence(img)
    assert not ev["marks"], ev
