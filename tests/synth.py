"""Synthetic scenes and videos for tests. Tests never use real footage."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import av
import cv2
import numpy as np
from numpy.typing import NDArray

from crossing_count.detector import Detection
from crossing_count.layout import Tile

Image = NDArray[np.uint8]
LIGHT_BLUE = (188, 171, 39)  # BGR of the RetailNext counting-line overlay


def texture(h: int, w: int, seed: int, mean: float = 140, spread: float = 60,
            blur: float = 4.0) -> Image:
    rng = np.random.default_rng(seed)
    base = rng.uniform(mean - spread, mean + spread, (h, w, 3)).astype(np.float32)
    blurred = cv2.GaussianBlur(base, (0, 0), blur)
    return np.clip(blurred, 0, 255).astype(np.uint8)


@dataclass
class Walker:
    keys: list[tuple[float, float, float]]  # (t, x, y) in frame pixels
    size: tuple[int, int] = (36, 44)
    color: tuple[int, int, int] = (40, 40, 70)

    def pos(self, t: float) -> tuple[float, float] | None:
        if t < self.keys[0][0] or t > self.keys[-1][0]:
            return None
        for (t0, x0, y0), (t1, x1, y1) in zip(self.keys, self.keys[1:]):
            if t0 <= t <= t1:
                f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
                return (x0 + (x1 - x0) * f, y0 + (y1 - y0) * f)
        return (self.keys[-1][1], self.keys[-1][2])


def render_frame(bg: Image, walkers: Iterable[Walker], t: float, rng: np.random.Generator,
                 noise: float = 2.0, overlay: Callable[[Image, float], None] | None = None) -> Image:
    img = np.clip(bg.astype(np.float32) + rng.normal(0, noise, bg.shape), 0, 255).astype(np.uint8)
    for w in walkers:
        p = w.pos(t)
        if p is not None:
            cv2.ellipse(img, (int(p[0]), int(p[1])), (w.size[0] // 2, w.size[1] // 2), 0, 0, 360,
                        w.color, -1, cv2.LINE_AA)
    if overlay is not None:
        overlay(img, t)
    return img


def write_video(path: Path, frames: Iterable[Image], fps: int = 10) -> None:
    with av.open(str(path), "w") as c:
        stream = None
        for img in frames:
            if stream is None:
                stream = c.add_stream("mpeg4", rate=fps)
                stream.width, stream.height = img.shape[1], img.shape[0]
                stream.pix_fmt = "yuv420p"
                stream.bit_rate = 12_000_000
            for pkt in stream.encode(av.VideoFrame.from_ndarray(img, format="bgr24")):
                c.mux(pkt)
        assert stream is not None
        for pkt in stream.encode():
            c.mux(pkt)


class Oracle:
    """A detector that returns each synthetic person's true position in one picture.

    Lets tests exercise tracking (real ByteTrack), stitching, the rule and the
    outputs without depending on YOLO.
    """

    name = "oracle"

    def __init__(self, walkers: list[Walker], tile: Tile) -> None:
        self.walkers, self.tile = walkers, tile

    def detect(self, frames: Sequence[Any], times: Sequence[float]) -> list[list[Detection]]:
        out = []
        for t in times:
            dets = []
            for w in self.walkers:
                p = w.pos(t)
                if p is not None and self.tile.contains(*p):
                    hw, hh = w.size[0] / 2, w.size[1] / 2
                    dets.append(Detection((p[0] - hw, p[1] - hh, p[0] + hw, p[1] + hh),
                                          (round(p[0], 1), round(p[1], 1)), 0.9))
            out.append(dets)
        return out
