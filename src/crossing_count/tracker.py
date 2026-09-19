"""ByteTrack (the Ultralytics implementation) over our own detections.

ByteTrack associates by box overlap from frame to frame. Its defaults are for
full person boxes; very small boxes lose their overlap when a person walks
10-15 px between frames at 10 fps, and tracks fragment. Swapped identities in
crowds are handled after tracking instead (tracks are cut where they jump
further than a person can walk; see tracks.split_at_jumps). Thresholds are low
on purpose: people near the line are often detected at 0.1-0.3 confidence here,
and a person reviews every proposal.

Which tracker follows people, and which matrix it associates on, are settings, so the
same recorded detections can be replayed under each of them and scored against a hand
count (bench.py --tracker/--assoc). The defaults are what the tool counts with; nothing
here is chosen on a measurement yet.
"""

from __future__ import annotations

import os

os.environ.setdefault("YOLO_OFFLINE", "1")

from types import SimpleNamespace
from typing import Any

import numpy as np
from numpy.typing import NDArray

from .detector import Detection

TRACKERS = ("byte", "botsort", "ocsort")
ASSOC = ("iou", "giou")
EMPTY_AREA = 1e-7  # a box with no width or height divides into nothing

# what each tracker needs beyond ByteTrack's own settings
EXTRA: dict[str, dict[str, Any]] = {
    # no camera motion to compensate (the cameras are screwed to the ceiling), and no
    # appearance model: re-identifying people needs the pictures, which a replay does not have
    "botsort": {"gmc_method": "none", "proximity_thresh": 0.5, "appearance_thresh": 0.25,
                "with_reid": False, "model": "auto"},
    # use_byte: OC-SORT's own second pass over the weak detections, so it is compared with
    # ByteTrack on equal terms. Our detections are deliberately faint; dropping that pass
    # would change more than the tracker.
    "ocsort": {"delta_t": 3, "inertia": 0.2, "use_byte": True},
}


def _corners(tracks: list[Any]) -> NDArray[np.float32]:
    if not tracks:
        return np.zeros((0, 4), dtype=np.float32)
    return np.ascontiguousarray([t.xyxy for t in tracks], dtype=np.float32)


def giou_distance(tracks: list[Any], detections: list[Any]) -> NDArray[np.float32]:
    """Cost by generalised overlap, on the same 0..1 scale as matching.iou_distance.

    Plain overlap is the same 0 for every box that misses, so two boxes that do not touch
    at all cannot be told apart. GIoU keeps measuring past that, through the smallest
    rectangle holding both, so a small box that jumped between frames is still ranked
    against the right track. Halved because GIoU runs -1..1 where overlap runs 0..1, so one
    match threshold means the same thing whichever matrix is in use.
    """
    a, b = _corners(tracks), _corners(detections)
    if not len(a) or not len(b):
        return np.zeros((len(a), len(b)), dtype=np.float32)
    lo = np.maximum(a[:, None, :2], b[None, :, :2])
    hi = np.minimum(a[:, None, 2:], b[None, :, 2:])
    inter = np.prod(np.clip(hi - lo, 0.0, None), axis=2)
    areas = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1]), (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    union = np.maximum(areas[0][:, None] + areas[1][None, :] - inter, EMPTY_AREA)
    holds = np.maximum(a[:, None, 2:], b[None, :, 2:]) - np.minimum(a[:, None, :2], b[None, :, :2])
    hull = np.maximum(np.prod(np.clip(holds, 0.0, None), axis=2), EMPTY_AREA)
    giou = inter / union - (hull - union) / hull
    out: NDArray[np.float32] = ((1.0 - giou) / 2.0).astype(np.float32)
    return out


def tracker_class(kind: str, assoc: str) -> type:
    """The Ultralytics tracker to run, with our association matrix if it is not the usual one."""
    from ultralytics.trackers.bot_sort import BOTSORT
    from ultralytics.trackers.byte_tracker import BYTETracker
    from ultralytics.trackers.oc_sort import OCSORT
    from ultralytics.trackers.utils import matching

    if kind not in TRACKERS:
        raise ValueError(f"tracker must be one of {', '.join(TRACKERS)}")
    if assoc not in ASSOC:
        raise ValueError(f"assoc must be one of {', '.join(ASSOC)}")
    base: type = {"byte": BYTETracker, "botsort": BOTSORT, "ocsort": OCSORT}[kind]
    if assoc == "iou":
        return base
    if kind != "byte":
        raise ValueError("assoc='giou' goes with tracker='byte': BoT-SORT and OC-SORT add "
                         "their own terms to the matrix, which this would drop")

    class GeneralisedOverlap(base):  # type: ignore[misc]
        """Only the first association changes: ByteTrack's second pass over the weak
        detections stays on plain overlap, as its authors intended."""

        def get_dists(self, tracks: list[Any], detections: list[Any]) -> NDArray[np.float32]:
            dists = giou_distance(tracks, detections)
            if self.args.fuse_score:
                dists = matching.fuse_score(dists, detections)
            return dists

    return GeneralisedOverlap


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
                 match: float = 0.8, box: str = "full", kind: str = "byte",
                 assoc: str = "iou") -> None:
        if box not in ("full", "compact"):
            raise ValueError("box must be 'full' or 'compact'")
        self.box = box
        self.kind, self.assoc = kind, assoc
        cls = tracker_class(kind, assoc)
        args = SimpleNamespace(
            track_high_thresh=high, track_low_thresh=low, new_track_thresh=new,
            track_buffer=max(1, int(round(lost_buffer_s * analysed_fps))),
            match_thresh=match, fuse_score=True, **EXTRA.get(kind, {}),
        )
        self._bt = cls(args)

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
