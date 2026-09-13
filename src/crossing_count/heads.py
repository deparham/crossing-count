"""Marking heads on camera pictures, to train a detector for overhead views.

From above, heads rarely overlap even in a crowd, so a detector that finds heads
loses fewer people in a busy doorway than one trained on side-on photos of whole
bodies. Frames come from busy moments of the stores' own videos, with the current
detector's people already marked at their heads, so a person mostly corrects.

Each frame is a camera picture (JPEG) plus a JSON file under labels/frames/ in the
data folder: the detector's guesses ("prefill", kept untouched for comparison) and
the person's marks ("heads", circles [x, y, r] in the picture's own pixels).
"""

from __future__ import annotations

import json
import pickle
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from . import layout as lay
from . import paths
from . import video as vid
from .gating import camera_dir
from .util import default_run_dir, slugify, write_json_atomic

TARGET_HEADS = 1000
DEFAULT_R = 14.0  # head radius in a 640x480 picture
HEAD_ALONG = 1.8  # head = feet + 1.8 x (box centre - feet): the far end of a standing body
MIN_GAP_S = 5.0  # frames from one camera at least this far apart
BUSY_SHARE = 0.8  # most frames from the busiest moments, the rest spread evenly
_ID = re.compile(r"^[a-z0-9_-]{1,200}$")


class LabelError(Exception):
    """A frame or request the marking page cannot use."""


def labels_root() -> Path:
    return paths.data_root() / "labels"


def head_points(dets: list[Any], tile: dict[str, int], merge_px: float = 1.5 * DEFAULT_R
                ) -> list[list[float]]:
    """Where the current detector's people have their heads, in the picture's own pixels."""
    w, h = tile["x1"] - tile["x0"], tile["y1"] - tile["y0"]
    out: list[list[float]] = []
    for d in sorted(dets, key=lambda d: -float(getattr(d, "conf", 0.0))):
        poly = d.polygon() if hasattr(d, "polygon") else np.array(
            [[d.bbox[0], d.bbox[1]], [d.bbox[2], d.bbox[3]]], dtype=np.float32)
        centre = np.asarray(poly, dtype=np.float64).mean(axis=0)
        foot = np.asarray(d.foot, dtype=np.float64)
        head = foot + HEAD_ALONG * (centre - foot)
        x, y = float(head[0] - tile["x0"]), float(head[1] - tile["y0"])
        if not (0 <= x < w and 0 <= y < h):
            continue
        if all(np.hypot(x - p[0], y - p[1]) > merge_px for p in out):  # one mark per person
            out.append([round(x, 1), round(y, 1), DEFAULT_R])
    return out


def busy_times(recorded: dict[float, list[Any]], duration: float, n: int) -> list[float]:
    """Moments to mark: mostly the busiest (most people found), some spread evenly."""
    chosen: list[float] = []

    def free(t: float) -> bool:
        return all(abs(t - c) >= MIN_GAP_S for c in chosen)

    by_people = sorted(recorded, key=lambda t: (-len(recorded[t]), t))
    for t in by_people:
        if len(chosen) >= round(n * BUSY_SHARE):
            break
        if recorded[t] and free(t):
            chosen.append(float(t))
    for t in np.linspace(2.0, max(2.0, duration - 2.0), max(2, 3 * n)):
        if len(chosen) >= n:
            break
        if free(float(t)):
            chosen.append(round(float(t), 2))
    return sorted(chosen)


def _camera_names(run: Path, fingerprint: str, n: int) -> dict[int, str]:
    names = {i: f"picture-{i + 1}" for i in range(n)}
    try:
        layout = json.loads((run / "layout.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return names
    if layout.get("fingerprint") == fingerprint:
        for c in layout.get("cameras", []):
            if isinstance(c.get("picture"), int) and c["picture"] in names and c.get("sensor"):
                names[c["picture"]] = str(c["sensor"])
    return names


def add_video(video: str | Path, per_camera: int = 30, root: Path | None = None,
              runs_root: Path | None = None) -> int:
    """Add frames of each camera in the video to mark; returns how many were new."""
    video = Path(video)
    folder = (root or labels_root()) / "frames"
    folder.mkdir(parents=True, exist_ok=True)
    info = vid.probe(video)
    audit = vid.audit_timebase(video)
    median = vid.median_of_video(video, audit.duration_s, interval_s=audit.median_interval_s)
    tiles = lay.detect_tiles_in(median)
    run = (runs_root or paths.data_root()) / default_run_dir(video, None)
    names = _camera_names(run, info.fingerprint, len(tiles))
    added = 0
    for tile in tiles:
        name = names[tile.index]
        recorded: dict[float, list[Any]] = {}
        pkl = camera_dir(run, name) / "detections.pkl"
        if pkl.is_file():
            try:  # written by detect.py --record on this computer
                recorded = pickle.loads(pkl.read_bytes()).get("frames", {})
            except (pickle.UnpicklingError, EOFError, AttributeError, ImportError):
                recorded = {}
        keys = np.array(sorted(recorded)) if recorded else np.zeros(0)
        box = tile.as_dict()
        for f in vid.grab_frames_at(video, busy_times(recorded, audit.duration_s, per_camera)):
            fid = f"{slugify(video.stem)}__{slugify(name)}__{int(round(f.t * 1000)):08d}"
            if (folder / f"{fid}.json").exists():
                continue  # already there: never overwrite someone's marks
            prefill: list[list[float]] = []
            if len(keys):
                k = keys[int(np.argmin(np.abs(keys - f.t)))]
                if abs(k - f.t) <= 0.06:
                    prefill = head_points(list(recorded[float(k)]), box)
            cv2.imwrite(str(folder / f"{fid}.jpg"), tile.crop(f.image), [cv2.IMWRITE_JPEG_QUALITY, 92])
            write_json_atomic(folder / f"{fid}.json", {
                "id": fid, "video": info.filename, "fingerprint": info.fingerprint,
                "camera": name, "picture": tile.index, "t": round(f.t, 3),
                "size": [tile.width, tile.height], "prefill": prefill, "heads": prefill,
                "prefilled": bool(len(keys)), "done": False, "skipped": False,
                "added_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            })
            added += 1
    return added


def match_heads(found: list[tuple[float, float]], marked: list[list[float]]
                ) -> tuple[int, int, int]:
    """(found and marked, found but not marked, marked but not found), nearest first."""
    pairs = sorted((float(np.hypot(f[0] - m[0], f[1] - m[1])), i, j)
                   for i, f in enumerate(found) for j, m in enumerate(marked)
                   if np.hypot(f[0] - m[0], f[1] - m[1]) <= max(1.5 * m[2], 12.0))
    used_f: set[int] = set()
    used_m: set[int] = set()
    for _, i, j in pairs:
        if i not in used_f and j not in used_m:
            used_f.add(i)
            used_m.add(j)
    return len(used_f), len(found) - len(used_f), len(marked) - len(used_m)


class HeadLabels:
    """The marked frames in one folder."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or labels_root()
        self.frames_dir = self.root / "frames"
        self.frames_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, fid: str, ext: str) -> Path:
        if not _ID.match(fid):
            raise LabelError(f"bad frame id {fid!r}")
        p = self.frames_dir / f"{fid}.{ext}"
        if not p.is_file():
            raise LabelError(f"no frame {fid}")
        return p

    def ids(self) -> list[str]:
        return sorted(p.stem for p in self.frames_dir.glob("*.json"))

    def get(self, fid: str) -> dict[str, Any]:
        data = json.loads(self._path(fid, "json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def image(self, fid: str) -> Path:
        return self._path(fid, "jpg")

    def save(self, fid: str, heads: list[list[float]], done: bool = True,
             skipped: bool = False) -> dict[str, Any]:
        frame = self.get(fid)
        w, h = frame["size"]
        clean: list[list[float]] = []
        for p in heads:
            if len(p) < 2:
                raise LabelError("each head needs x and y")
            x, y = float(p[0]), float(p[1])
            r = float(p[2]) if len(p) > 2 else DEFAULT_R
            if not (0 <= x <= w and 0 <= y <= h):
                raise LabelError(f"a head at ({x:.0f}, {y:.0f}) is outside the picture")
            clean.append([round(x, 1), round(y, 1), round(min(60.0, max(4.0, r)), 1)])
        frame.update(heads=clean, done=bool(done and not skipped), skipped=bool(skipped),
                     saved_at=datetime.now().astimezone().isoformat(timespec="seconds"))
        write_json_atomic(self._path(fid, "json"), frame)
        return frame

    def summary(self) -> list[dict[str, Any]]:
        out = []
        for fid in self.ids():
            f = self.get(fid)
            out.append({"id": fid, "video": f["video"], "camera": f["camera"], "t": f["t"],
                        "done": f["done"], "skipped": f["skipped"], "heads": len(f["heads"])})
        return out

    def stats(self) -> dict[str, Any]:
        rows = self.summary()
        done = [r for r in rows if r["done"]]
        return {"frames": len(rows), "done": len(done),
                "skipped": sum(1 for r in rows if r["skipped"]),
                "heads": sum(r["heads"] for r in done), "target": TARGET_HEADS,
                "videos": sorted({r["video"] for r in rows})}

    def export_yolo(self, out: Path, val_share: float = 0.2) -> dict[str, Any]:
        """A YOLO training set of the finished frames: one class, "head".

        Frames kept for checking come from other videos where possible, so the check
        says how the detector does on footage it never saw.
        """
        done = [self.get(r["id"]) for r in self.summary() if r["done"]]
        if not done:
            raise LabelError("No finished frames to train on yet.")
        videos = sorted({f["video"] for f in done})
        if len(videos) >= 3:  # whole videos held out, about val_share of the frames
            per = {v: sum(1 for f in done if f["video"] == v) for v in videos}
            val_videos: set[str] = set()
            for v in sorted(videos, key=lambda v: (per[v], v)):
                if sum(per[x] for x in val_videos) + per[v] <= max(1, val_share * len(done)):
                    val_videos.add(v)
            is_val = [f["video"] in val_videos for f in done]
        else:  # every fifth frame
            is_val = [k % round(1 / val_share) == 0 for k in range(len(done))]
        shutil.rmtree(out, ignore_errors=True)
        counts = {"train": 0, "val": 0, "heads": 0}
        for f, val in zip(done, is_val, strict=True):
            split = "val" if val else "train"
            (out / "images" / split).mkdir(parents=True, exist_ok=True)
            (out / "labels" / split).mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.image(f["id"]), out / "images" / split / f"{f['id']}.jpg")
            w, h = f["size"]
            lines = [f"0 {x / w:.6f} {y / h:.6f} {2 * r / w:.6f} {2 * r / h:.6f}"
                     for x, y, r in f["heads"]]
            (out / "labels" / split / f"{f['id']}.txt").write_text(
                "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            counts[split] += 1
            counts["heads"] += len(lines)
        yaml = out / "dataset.yaml"
        yaml.write_text(f"path: {out}\ntrain: images/train\nval: images/val\nnames:\n  0: head\n",
                        encoding="utf-8")
        return {"yaml": str(yaml), **counts, "val_ids": [f["id"] for f, v in zip(done, is_val,
                                                                              strict=True) if v]}
