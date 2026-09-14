"""Learning examples: every counted crossing saved as frames plus a description.

Each crossing a person counted by hand, confirmed, or added becomes a positive
example; checks answered "no" and random watched moments with nobody crossing
become negatives. Together they are the data for measuring the automatic
counter on these stores' cameras and, later, retraining it. They hold CCTV
frames of people, so they go only to the folder chosen in the app's settings.

Layout: <folder>/<store>/<video>/<camera>/<label>_<n>/ with frame_*.jpg (the
camera's picture from 1.5 s before to 1 s after the moment) and meta.json;
<folder>/index.jsonl gets one line per example. The whole validation (answers, counts by
hand, the sensor's numbers, the decision log: no video) goes to
<folder>/validations/<store>/<video>.json.

The folder can be shared by a team (a SharePoint library synced by OneDrive, a network
share), so everyone's work lands in one place. Each example names its store's set: an
example from a test-set store says train_ok false and must never be trained on, or the
test set's results would mean nothing.
"""

from __future__ import annotations

import json
import random
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import cv2

from . import video as vid
from .util import slugify, write_json_atomic

if TYPE_CHECKING:
    from .wizard import Wizard

SCHEMA = "example/1"
BEFORE_S, AFTER_S, STRIDE = 1.5, 1.0, 2  # frames from 1.5 s before to 1 s after, every 2nd
NEGATIVE_GAP_S = 4.0  # a moment with nobody crossing is at least this far from any count


def check_folder(path: str) -> Path:
    """The examples folder, created if needed and checked for writing."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ValueError("Give the full path of the folder, e.g. \\\\server\\share\\examples.")
    p.mkdir(parents=True, exist_ok=True)
    probe = p / ".write_test"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    return p


def _in_folder(line: str, prefix: str) -> bool:
    try:
        path = str(json.loads(line).get("path", ""))
    except ValueError:
        return False
    return path == prefix or path.startswith((prefix + "/", prefix + "\\"))


def moments(w: Wizard) -> list[dict[str, Any]]:
    """What to save: positives and negatives, each with its time, camera and source."""
    st = w.state
    out: list[dict[str, Any]] = []
    if w.manual():
        m = st["manual"]
        for c in m["counts"]:
            out.append({"label": "crossing", "source": "manual_click", "camera": c["camera"],
                        "t": float(c["t"]), "direction": c["direction"]})
        rng = random.Random(st["fingerprint"])
        for cam in st["cameras"]:
            key = cam["sensor"]
            clicks = [float(c["t"]) for c in m["counts"] if c["camera"] == key]
            pool = [float(t) for a, b in m["watched"].get(key, [])
                    for t in range(int(a) + 1, int(b))
                    if all(abs(t - x) >= NEGATIVE_GAP_S for x in clicks)]
            for t in sorted(rng.sample(pool, min(len(pool), max(5, len(clicks))))):
                out.append({"label": "no_crossing", "source": "random_watched_moment",
                            "camera": key, "t": t, "direction": None})
        return out
    answers = st["answers"]
    for it in w.check_items():
        a = answers.get(it["id"])
        if a not in ("yes", "no"):  # unanswered or unsure: nothing certain to learn from
            continue
        what = "detection" if it["kind"] == "counted" else "possible_miss"
        out.append({"label": "crossing" if a == "yes" else "no_crossing",
                    "source": f"{'confirmed' if a == 'yes' else 'rejected'}_{what}",
                    "camera": it["camera"], "t": float(it["t"]), "direction": it["direction"],
                    "people": int(st.get("people", {}).get(it["id"], 1)) if a == "yes" else 0,
                    "detection": {"id": it["id"], "why": it["why"], "point": it["point"],
                                  "path": it["path"]}})
    for a in st["added"]:
        out.append({"label": "crossing", "source": "added_by_checker", "camera": a["camera"],
                    "t": float(a["t"]), "direction": a["direction"]})
    return out


def validation_record(w: Wizard) -> dict[str, Any]:
    """The whole validation as data, without the video: for the team's shared folder."""
    from .gold import split_of
    from .version import app_version

    st = w.state
    code = str(st["store"].get("code") or "")
    return {
        "schema": "validation-record/1", "saved_at": datetime.now().astimezone().isoformat(
            timespec="seconds"), "app_version": app_version(),
        "store": st["store"], "split": split_of(code) if code else None,
        "video": {"filename": st["filename"], "fingerprint": st["fingerprint"],
                  "duration_s": st["duration_s"], "clock_start": st["clock_start"],
                  "clock_end": st.get("clock_end"), "tz": st.get("tz")},
        "mode": st.get("mode") or "auto", "marked": bool(st.get("marks")),
        "cameras": [{k: c.get(k) for k in ("sensor", "picture", "site", "config")}
                    for c in st["cameras"]],
        "direction": st["direction"], "rules": st.get("rules"),
        "specification": st.get("manual", {}).get("specification"),
        "sensor": st["sensor"], "sensor_intervals": st.get("sensor_intervals"),
        "sensor_cameras": st.get("sensor_cameras"), "sensor_source": st.get("sensor_source"),
        "model": st.get("model"), "job": {k: st["job"].get(k) for k in ("status", "started_at",
                                                                         "finished_at")},
        "counts": w.counts(), "answers": st["answers"], "people": st.get("people", {}),
        "added": st["added"], "watched": st["watched"], "watch": st.get("watch"),
        "manual": {k: st["manual"].get(k) for k in ("counts", "watched", "done", "specification")},
        "decisions": st.get("decisions", []), "report": st.get("report"),
    }


def export_examples(w: Wizard, root: Path) -> dict[str, Any]:
    from .gold import split_of

    st, store = w.state, w.state["store"]
    split = split_of(str(store.get("code") or "")) if store.get("code") else None
    write_json_atomic(root / "validations" / slugify(store.get("code") or "site")
                      / f"{slugify(Path(st['filename']).stem)}.json", validation_record(w))
    geometry = w.geometry()
    video_dir = root / slugify(store.get("code") or "site") / slugify(Path(st["filename"]).stem)
    shutil.rmtree(video_dir, ignore_errors=True)  # saving again replaces this video's examples
    start = datetime.fromisoformat(st["clock_start"]) if st["clock_start"] else None
    export_id = datetime.now().astimezone().isoformat(timespec="seconds")
    duration = float(st["duration_s"])
    lines: list[str] = []
    numbers: dict[tuple[str, str], int] = {}
    for mo in moments(w):
        g = geometry[mo["camera"]]
        tile = g["tile"]
        ox, oy = tile["x0"], tile["y0"]

        def local(pts: Any, ox: float = ox, oy: float = oy) -> Any:
            """Video pixels to the camera picture's own pixels."""
            return [[round(x - ox, 1), round(y - oy, 1)] for x, y in pts] if pts else None

        key = (mo["camera"], mo["label"])
        numbers[key] = numbers.get(key, 0) + 1
        folder = video_dir / slugify(mo["camera"]) / f"{mo['label']}_{numbers[key]:04d}"
        folder.mkdir(parents=True, exist_ok=True)
        frames = []
        a, b = max(0.0, mo["t"] - BEFORE_S), min(duration, mo["t"] + AFTER_S)
        for k, f in enumerate(vid.iter_range(w.video, a, b, stride=STRIDE)):
            name = f"frame_{k:02d}.jpg"
            cv2.imwrite(str(folder / name), f.image[tile["y0"]:tile["y1"], tile["x0"]:tile["x1"]],
                        [cv2.IMWRITE_JPEG_QUALITY, 88])
            frames.append({"file": name, "t": round(f.t, 3)})
        when = start + timedelta(seconds=mo["t"]) if start else None
        meta: dict[str, Any] = {
            "schema": SCHEMA, "label": mo["label"], "direction": mo["direction"],
            "source": mo["source"], "camera": mo["camera"], "t_seconds": round(mo["t"], 3),
            "clock": when.strftime("%H:%M:%S") if when else None,
            "date": when.strftime("%Y-%m-%d") if when else None,
            "video": st["filename"], "fingerprint": st["fingerprint"],
            "store_code": store.get("code", ""), "store_name": store.get("name", ""),
            "operator": store.get("operator", ""), "mode": st.get("mode") or "auto",
            "tile_size": [tile["x1"] - tile["x0"], tile["y1"] - tile["y0"]],
            "line": local(g["line"]), "mask": local(g["mask"]), "frames": frames,
            "export_id": export_id, "split": split, "train_ok": split != "test",
            "rules": st.get("rules"), "marked": bool(st.get("marks")),
        }
        if "people" in mo:  # how many crossed together at this moment
            meta["people"] = mo["people"]
        if "detection" in mo:
            det = mo["detection"]
            meta["detection"] = {**det, "point": local([det["point"]])[0] if det["point"] else None,
                                 "path": [[p[0], *local([p[1:3]])[0]] for p in det["path"]]
                                 if det["path"] else None}
        write_json_atomic(folder / "meta.json", meta)
        lines.append(json.dumps({
            "path": folder.relative_to(root).as_posix(), "label": mo["label"],
            "direction": mo["direction"], "source": mo["source"], "camera": mo["camera"],
            "video": st["filename"], "t_seconds": round(mo["t"], 3), "export_id": export_id,
            "store_code": store.get("code", ""), "split": split, "train_ok": split != "test"},
            ensure_ascii=False))
    # The index keeps one entry per example: this video's earlier entries are replaced.
    index = root / "index.jsonl"
    prefix = video_dir.relative_to(root).as_posix()  # one form on Mac and Windows alike
    try:
        kept = [line for line in index.read_text(encoding="utf-8").splitlines()
                if line.strip() and not _in_folder(line, prefix)]
    except OSError:
        kept = []
    if kept or lines:
        tmp = index.with_name(index.name + ".tmp")
        tmp.write_text("\n".join([*kept, *lines]) + "\n", encoding="utf-8")
        tmp.replace(index)
    positives = sum(1 for line in lines if '"label": "crossing"' in line)
    return {"count": len(lines), "positives": positives, "negatives": len(lines) - positives,
            "folder": str(video_dir)}
