"""Is our counting line where the sensor's is? And which camera is this picture?

On footage showing RetailNext's marks, a line drawn over RetailNext's burned-in line is its
line by construction. On clean footage there is nothing to draw over, and a line drawn half
a metre off measures something the sensor does not: the difference then shows up as sensor
error that is not sensor error. So every drawing records how its line was placed, strongest
first:

    api          fetched from RetailNext's API. Not available: its documented API describes
                 locations (stores, entrances, time zones), not the lines they count on
    calibrated   drawn over RetailNext's own line on its marked footage, then used on clean
                 footage of the same camera
    by_eye       drawn by eye on clean footage: the weakest evidence

(A count by hand on marked footage uses RetailNext's line in the picture itself:
sensor_line, which corresponds by construction but is not independent.)

A drawing made on marked footage keeps the people-free picture it was drawn on
(<config>.ref.jpg). Clean footage is matched to it by comparing the pictures with the marks
masked out (alignment): the same camera, unmoved, lines up; a moved camera or another
camera does not, and the drawing is then not used without a person confirming.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

from . import overlay as ov

Image = NDArray[np.uint8]
METHODS = {"api": "fetched from RetailNext's API",
           "calibrated": "calibrated on RetailNext's own line",
           "sensor_line": "RetailNext's own line, burned into the picture",
           "by_eye": "drawn by eye on clean footage"}
STRENGTH = {"api": 3, "calibrated": 2, "sensor_line": 2, "by_eye": 1}
MIN_LINE_MATCH = 0.8  # share of a drawn line on the burned-in line, to count as calibrated
MAX_SHIFT_PX = 6.0  # per 640-pixel width: more and the camera has moved (or is another one)
MIN_SIMILARITY = 0.4  # edges of the two pictures, marks masked out (tuned on real exports)


def of_drawing(line_match: float | None, picture_marked: bool) -> dict[str, Any]:
    """How a line just drawn corresponds to the sensor's."""
    if line_match is not None and line_match >= MIN_LINE_MATCH:
        return {"method": "calibrated", "line_match": round(line_match, 3),
                "words": f"calibrated on RetailNext's own line ({line_match:.0%} of it on the "
                         f"burned-in line)"}
    if picture_marked:
        return {"method": "by_eye", "line_match": line_match,
                "words": "drawn by eye, not on RetailNext's burned-in line although the picture "
                         "shows it"}
    return {"method": "by_eye", "line_match": None, "words": METHODS["by_eye"]}


def of_config(traced_on: dict[str, Any] | None, overlay_hsv: Any) -> dict[str, Any]:
    """How a saved drawing's line corresponds; drawings older than this record say by their
    recorded overlay colour whether they were drawn over RetailNext's line."""
    rec = (traced_on or {}).get("correspondence")
    if isinstance(rec, dict) and rec.get("method") in METHODS:
        return dict(rec)
    if overlay_hsv is not None:
        return {"method": "calibrated", "line_match": None,
                "words": "drawn over RetailNext's own line (an earlier drawing: how much of it "
                         "was on the line is not recorded)"}
    return {"method": "by_eye", "line_match": None, "words": METHODS["by_eye"]}


def reference_path(config: Path) -> Path:
    return config.with_name(config.stem + ".ref.jpg")


def save_reference(config: Path, picture: Image) -> Path:
    p = reference_path(config)
    cv2.imwrite(str(p), picture, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return p


def load_reference(config: Path) -> Image | None:
    p = reference_path(config)
    img = cv2.imread(str(p)) if p.is_file() else None
    return None if img is None else np.asarray(img, dtype=np.uint8)


def _marks(img: Image) -> NDArray[np.bool_]:
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    m = np.zeros(img.shape[:2], bool)
    for lo, hi, smin, vmin in ov.MARK_BANDS:
        m |= (h >= lo) & (h <= hi) & (s >= smin) & (v >= vmin)
    return m


def _edges(img: Image) -> NDArray[np.float32]:
    g = cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32), (5, 5), 1.5)
    return np.asarray(cv2.magnitude(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1)),
                      dtype=np.float32)


def alignment(ref: Image, img: Image) -> dict[str, Any]:
    """Is img the same, unmoved camera view as ref? Both people-free pictures of one camera
    (no header bar); RetailNext's marks in either are left out of the comparison."""
    if abs(ref.shape[1] / ref.shape[0] - img.shape[1] / img.shape[0]) > 0.02:
        return {"aligned": False, "shift_px": None, "similarity": None,
                "reason": "a differently shaped picture"}
    if img.shape[:2] != ref.shape[:2]:
        img = np.asarray(cv2.resize(img, (ref.shape[1], ref.shape[0]),
                                    interpolation=cv2.INTER_AREA), dtype=np.uint8)
    h, w = ref.shape[:2]
    scale = w / 640.0
    kernel = np.ones((9, 9), np.uint8)
    masked = cv2.dilate((_marks(ref) | _marks(img)).astype(np.uint8), kernel) > 0
    a, b = _edges(ref), _edges(img)
    a[masked], b[masked] = 0.0, 0.0
    win = cv2.createHanningWindow((w, h), cv2.CV_32F)
    (dx, dy), _ = cv2.phaseCorrelate(a, b, win)
    back = np.asarray([[1.0, 0.0, -dx], [0.0, 1.0, -dy]], dtype=np.float32)
    moved = np.asarray(cv2.warpAffine(b, back, (w, h)), dtype=np.float32)
    border = max(8, int(abs(dx)) + 4, int(abs(dy)) + 4)
    keep = ~masked
    keep[:border], keep[-border:], keep[:, :border], keep[:, -border:] = False, False, False, False
    x, y = a[keep], moved[keep]
    sim = float(np.corrcoef(x, y)[0, 1]) if x.size > 100 and x.std() > 0 and y.std() > 0 else 0.0
    shift = float(np.hypot(dx, dy)) / scale
    return {"aligned": shift <= MAX_SHIFT_PX and sim >= MIN_SIMILARITY,
            "shift_px": round(shift, 1), "similarity": round(sim, 3), "reason": None}
