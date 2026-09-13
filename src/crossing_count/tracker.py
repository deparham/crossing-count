"""ByteTrack (the Ultralytics implementation) over our own detections.

ByteTrack associates by box overlap from frame to frame. Its defaults are for
full person boxes; very small boxes lose their overlap when a person walks
10-15 px between frames at 10 fps, and tracks fragment. Swapped identities in
crowds are handled after tracking instead (tracks are cut where they jump
further than a person can walk; see tracks.split_at_jumps). Thresholds are low
on purpose: people near the line are often detected at 0.1-0.3 confidence here,
and a person reviews every proposal.
"""

from __future__ import annotations

import os

os.environ.setdefault("YOLO_OFFLINE", "1")

from types import SimpleNamespace

import numpy as np
from numpy.typing import NDArray

from .detector import Detection


class _Detections:
    """The minimal results-like object BYTETracker.update() reads."""

    def __init__(self, xywh: NDArray[np.float32], conf: NDArray[np.float32],
                 cls: NDArray[np.float32]) -> None:
        self.xywh, self.conf, self.cls = xywh, conf, cls

    def __len__(self) -> int:
        return len(self.conf)

    def __getitem__(self, mask: NDArray[np.bool_]) -> _Detections:
        return _Detections(self.xywh[mask], self.conf[mask], self.cls[mask])


class ByteTrackAdapter:
    def __init__(self, analysed_fps: float, lost_buffer_s: float = 2.0,
                 high: float = 0.2, low: float = 0.08, new: float = 0.2,
                 match: float = 0.8, box: str = "full") -> None:
        from ultralytics.trackers.byte_tracker import BYTETracker

        if box not in ("full", "compact"):
            raise ValueError("box must be 'full' or 'compact'")
        self.box = box
        args = SimpleNamespace(
            track_high_thresh=high, track_low_thresh=low, new_track_thresh=new,
            track_buffer=max(1, int(round(lost_buffer_s * analysed_fps))),
            match_thresh=match, fuse_score=True,
        )
        self._bt = BYTETracker(args)  # type: ignore[no-untyped-call]

    def update(self, dets: list[Detection]) -> list[tuple[int, int]]:
        """Feed one frame. Returns (tracker id, detection index) for tracks seen this frame."""
        if dets:
            boxes = [(d.track_box or d.bbox) if self.box == "compact" else d.bbox for d in dets]
            b = np.array(boxes, dtype=np.float32)
            xywh = np.stack([(b[:, 0] + b[:, 2]) / 2, (b[:, 1] + b[:, 3]) / 2,
                             b[:, 2] - b[:, 0], b[:, 3] - b[:, 1]], axis=1)
            conf = np.array([d.conf for d in dets], dtype=np.float32)
        else:
            xywh = np.zeros((0, 4), dtype=np.float32)
            conf = np.zeros(0, dtype=np.float32)
        out = self._bt.update(_Detections(xywh, conf, np.zeros(len(conf), dtype=np.float32)))
        return sorted((int(r[4]), int(r[7])) for r in out)
