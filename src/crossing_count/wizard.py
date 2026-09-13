"""Guided count: pick the footage, draw, count automatically, check, report.

The tool finds the crossings; a person then confirms each one it found and each
likely miss it lists (and can also watch the movement it could not explain), so
the report's number is a verified count. Detection runs gate.py and detect.py as
child processes, and their progress is read from what they print.
"""

from __future__ import annotations

import json
import os
import pickle
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

from . import paths
from . import video as vid
from .candidates import MISS_KIND
from .config import bind, load_config
from .export import sensor_accuracy
from .gating import camera_dir
from .manual import merge_ranges, unwatched_ranges
from .report_pptx import build_report
from .util import default_run_dir, fmt_hms, write_json_atomic

ROOT = paths.SOURCE_ROOT  # the project folder, when running from source
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".mkv", ".avi")
DIRECTIONS = ("in", "out")
CHOICES = {"in": ["in"], "out": ["out"], "both": ["in", "out"]}
LABELS = {"in": "Traffic In", "out": "Traffic Out"}
MODELS = ("yolo11m.pt", "yolo11s.pt")
TWIN_WINDOW_S = 2.0  # same direction this close on another camera: maybe one person seen twice
# Rule rejections offered to the checker. Measured on checked clips (11:30 CN-123, YD-612):
# "never touched the filter zone" and "out and back on one track" were real about half the
# time or more; exits without the mask 3 in 7; entries lost before the mask 0 in 8; tracks
# broken at the line 9 in 94. U-turns (0 in 2) stay rejected.
POSSIBLE_REASONS = ("no_filter", "returned_same_track", "outward_no_mask", "pending_expired",
                    "pending_at_eof", "pending_at_range_end")
PROMPT_PRIORITY = {"no_filter": 0, "returned_same_track": 0, "outward_no_mask": 0,
                   "pending_expired": 1, "pending_at_eof": 1, "pending_at_range_end": 1, "lost": 2}


def prompt_priority(why: str) -> int:
    """Possible misses most often real come first; tracks broken at the line last."""
    return PROMPT_PRIORITY.get(why, 1)
CLIP_BEFORE_S = 2.5
CLIP_AFTER_S = 1.5
FOUND = {  # how each verified crossing came to be counted, for the report
    "detected": "Detected, confirmed",
    "lost": "Track lost at line, confirmed",
    "added": "Added by the checker",
}
Image = NDArray[np.uint8]


class WizardError(Exception):
    """A step that cannot be done yet, or input that does not make sense."""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _pts(a: Any) -> list[list[float]]:
    return [[round(float(x), 1), round(float(y), 1)] for x, y in a]


def _near(path: list[list[float]] | None, t: float) -> list[float] | None:
    """The [x, y] of the path sample closest in time to t."""
    if not path:
        return None
    s = min(path, key=lambda p: abs(p[0] - t))
    return [s[1], s[2]]


def _iou(a: Any, b: Any) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def distinct_people(dets: list[Any], overlap: float = 0.3, near_px: float = 40.0) -> list[Any]:
    """One detection per person, for pictures: the rotated crops often find someone twice,
    as boxes that barely overlap but stand on the same spot."""
    kept: list[Any] = []
    for d in sorted(dets, key=lambda d: -float(getattr(d, "conf", 0.0))):
        foot = getattr(d, "foot", None)
        if all(_iou(d.bbox, k.bbox) < overlap
               and (foot is None or getattr(k, "foot", None) is None
                    or float(np.hypot(foot[0] - k.foot[0], foot[1] - k.foot[1])) > near_px)
               for k in kept):
            kept.append(d)
    return kept


def _dur(seconds: float) -> str:
    s = max(0, round(seconds))
    return f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"


def default_folders() -> list[Path]:
    home = Path.home()
    return [p for p in (home / "Downloads", home / "Desktop", home / "Movies", home / "Videos")
            if p.is_dir()]


def list_videos(folders: list[Path]) -> list[dict[str, Any]]:
    """Video files directly inside the folders, newest first."""
    found: list[dict[str, Any]] = []
    for folder in folders:
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.name.startswith(".") or p.suffix.lower() not in VIDEO_EXTS or not p.is_file():
                continue
            st = p.stat()
            found.append({
                "path": str(p), "name": p.name, "folder": str(folder),
                "size_mb": round(st.st_size / 1e6, 1),
                "modified": datetime.fromtimestamp(st.st_mtime, tz=UTC).astimezone()
                .isoformat(timespec="minutes"),
            })
    found.sort(key=lambda v: str(v["modified"]), reverse=True)
    return found


def mark_twins(items: list[dict[str, Any]]) -> None:
    """Flag checks that may be the same person as a counted crossing elsewhere.

    Two cameras can watch the same stretch of doorway. Of two counted crossings in
    the same direction within TWIN_WINDOW_S on different cameras, the later camera's
    one gets a note, as does a possible miss that coincides with a counted crossing,
    so the checker can answer no and count that person once.
    """
    counted = [i for i in items if i["kind"] == "counted"]
    for it in items:
        for other in counted:
            if (other is it or other["direction"] != it["direction"]
                    or abs(other["t"] - it["t"]) > TWIN_WINDOW_S):
                continue
            if it["kind"] == "counted" and other["picture"] >= it["picture"]:
                continue
            it["twin"] = {"id": other["id"], "camera": other["camera"], "t": other["t"]}
            break


# ---- following the child processes -------------------------------------------------------

_GATE = re.compile(r"gating (\d+):(\d+):([\d.]+) / (\d+):(\d+):([\d.]+)")
_TRACK = re.compile(r"tracking\s+([\d.]+)% of active time\s+\(\s*([\d.]+)x realtime\)")
_CAMERA = re.compile(r"^\s*(\S.*?): \w+ detector,")


def _secs(h: str, m: str, s: str) -> float:
    return int(h) * 3600 + int(m) * 60 + float(s)


@dataclass
class Progress:
    """What gate.py and detect.py are doing, read from their output."""

    cameras: int = 1
    stage: str = "starting"  # starting, gate, detect, done
    camera: str = ""
    seen: list[str] = field(default_factory=list)
    stage_pct: float = 0.0
    speed: float | None = None
    message: str = "Starting"
    log: list[str] = field(default_factory=list)

    def overall(self) -> float:
        """Percent of the whole job: the gate is quick (5%), detection is the rest."""
        if self.stage == "done":
            return 100.0
        if self.stage == "gate":
            return 5.0 * self.stage_pct / 100.0
        if self.stage == "detect":
            per = 95.0 / max(1, self.cameras)
            return 5.0 + per * (max(0, len(self.seen) - 1) + self.stage_pct / 100.0)
        return 0.0

    def feed(self, text: str) -> None:
        line = text.strip()
        if not line or line.startswith("objc["):
            return
        m = _GATE.search(line)
        if m:
            done, total = _secs(m[1], m[2], m[3]), _secs(m[4], m[5], m[6])
            self.stage = "gate"
            self.stage_pct = min(100.0, 100.0 * done / total) if total else 0.0
            self.message = (f"Finding movement near the line: {fmt_hms(done)[:8]} of "
                            f"{fmt_hms(total)[:8]}")
            return
        m = _TRACK.search(line)
        if m:
            self.stage, self.stage_pct, self.speed = "detect", float(m[1]), float(m[2])
            self.message = (f"Detecting and tracking people on {self.camera}: "
                            f"{self.stage_pct:.0f}% ({self.speed:.1f}x realtime)")
            return
        m = _CAMERA.match(text)
        if m:
            self.stage, self.camera, self.stage_pct = "detect", m[1], 0.0
            if m[1] not in self.seen:
                self.seen.append(m[1])
            self.message = f"Detecting and tracking people on {self.camera}"
        self.log.append(line)
        del self.log[:-300]


class _Job:
    """Runs the commands one after another in a thread, feeding their output to Progress."""

    def __init__(self, commands: list[list[str]], progress: Progress,
                 finish: Callable[[str, str | None, list[str]], None]) -> None:
        self.commands = commands
        self.progress = progress
        self._finish = finish
        self.started = time.monotonic()
        self.stopped = False
        self.running = True
        self._proc: subprocess.Popen[bytes] | None = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        status: str = "done"
        error: str | None = None
        cwd = paths.data_root()
        cwd.mkdir(parents=True, exist_ok=True)
        for cmd in self.commands:
            if self.stopped:
                break
            name = Path(cmd[1]).name if len(cmd) > 1 else cmd[0]
            try:
                proc = subprocess.Popen(cmd, cwd=cwd, env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as exc:
                status, error = "failed", f"could not start {name}: {exc}"
                break
            self._proc = proc
            assert proc.stdout is not None
            fd, pending = proc.stdout.fileno(), b""
            while True:
                data = os.read(fd, 4096)
                if not data:
                    break
                parts = re.split(rb"[\r\n]", pending + data)
                pending = parts.pop()
                for part in parts:
                    self.progress.feed(part.decode("utf-8", "replace"))
            if pending:
                self.progress.feed(pending.decode("utf-8", "replace"))
            code = proc.wait()
            if self.stopped:
                break
            if code != 0:
                status, error = "failed", (f"{name} stopped with an error (exit code {code}); "
                                           f"the log says why")
                break
        if self.stopped:
            status, error = "stopped", None
        if status == "done":
            self.progress.stage, self.progress.message = "done", "Finished"
        self.running = False
        self._finish(status, error, self.progress.log[-40:])

    def stop(self) -> None:
        self.stopped = True
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()


def tool(name: str) -> list[str]:
    """How to start one of the pipeline scripts: a sub-command of this program when installed."""
    if paths.FROZEN:
        return [sys.executable, name]
    return [sys.executable, str(ROOT / f"{name}.py")]


def pipeline_commands(w: Wizard) -> list[list[str]]:
    """gate.py then detect.py for the chosen cameras, each pinned to its picture."""
    cams = w.state["cameras"]
    cfgs = [c["config"] for c in cams]
    tiles = [a for c in cams for a in ("--tile", f"{c['sensor']}={c['picture']}")]
    out = ["--out-dir", str(w.run_dir)]
    return [
        [*tool("gate"), str(w.video), *cfgs, *tiles, *out],
        [*tool("detect"), str(w.video), *cfgs, "--model", w.state["model"], "--record", *out],
    ]


# ---- the count ---------------------------------------------------------------------------

class Wizard:
    """One video's guided count, saved on every change to runs/<video>/wizard/state.json."""

    def __init__(self, video: str | Path, sites_dir: str | Path | None = None,
                 runs_root: str | Path | None = None,
                 commands: Callable[[Wizard], list[list[str]]] | None = None) -> None:
        self.video = Path(video).expanduser().resolve()
        if not self.video.is_file():
            raise WizardError(f"There is no video at {self.video}")
        self.sites_dir = Path(sites_dir) if sites_dir else paths.sites_dir()
        root = Path(runs_root) if runs_root else paths.data_root()
        self.run_dir = root / default_run_dir(self.video, None)
        self.dir = self.run_dir / "wizard"
        self.path = self.dir / "state.json"
        self.stores_path = root / "runs" / "stores.json"
        self._commands = commands or pipeline_commands
        self._lock = threading.RLock()
        self._job: _Job | None = None
        info = vid.probe(self.video)
        if self.path.is_file():
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if state.get("fingerprint") != info.fingerprint:
                raise WizardError(f"{self.path} belongs to a different video with the same "
                                  f"name; move that folder away to start again")
            if state["job"]["status"] == "running":  # the app was closed during a count
                state["job"].update(status="stopped", error="The count was interrupted.")
            self.state: dict[str, Any] = state
        else:
            audit = vid.audit_timebase(self.video)
            interval = vid.parse_filename_interval(info.filename)
            self.state = {
                "schema": "wizard/1", "video": str(self.video), "filename": info.filename,
                "fingerprint": info.fingerprint, "duration_s": round(audit.duration_s, 3),
                "clock_start": interval.start if interval else None,
                "clock_end": interval.end if interval else None,
                "tz": interval.tz if interval else "",
                "cameras": [], "direction": None, "sensor": {}, "model": MODELS[0],
                "store": {"name": "", "code": "", "location": "Entrance", "report_date": ""},
                "job": {"status": "idle", "started_at": None, "finished_at": None,
                        "error": None, "log": []},
                "answers": {}, "added": [], "watched": [], "watch": None, "report": None,
                "version": 0, "created_at": _now(),
            }
            self._save()
        for key, value in (("mode", None), ("marks", None), ("examples", None),
                           ("manual", {"counts": [], "watched": {}, "positions": {},
                                       "done": False, "next_id": 1})):
            self.state.setdefault(key, value)
        self.state["store"].setdefault("operator", "")

    def _save(self) -> None:
        self.state["version"] = int(self.state.get("version", 0)) + 1
        write_json_atomic(self.path, self.state)

    def dirs(self) -> list[str]:
        return list(CHOICES.get(self.state["direction"] or "", []))

    def clock(self, t: float) -> str:
        cs = self.state["clock_start"]
        if not cs:
            return fmt_hms(t)[:8]
        return (datetime.fromisoformat(cs) + timedelta(seconds=t)).strftime("%H:%M:%S")

    def _camera(self, sensor: str) -> dict[str, Any]:
        for c in self.state["cameras"]:
            if c["sensor"] == sensor:
                return dict(c)
        raise WizardError(f"{sensor} is not one of this count's cameras")

    def _read(self, cam: dict[str, Any], name: str) -> dict[str, Any]:
        try:
            data = json.loads((camera_dir(self.run_dir, cam["sensor"]) / f"{name}.json")
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _stores(self) -> dict[str, str]:
        try:
            data = json.loads(self.stores_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    # ---- steps before counting ---------------------------------------------------------

    def set_cameras(self, existing: list[dict[str, Any]], tiles: list[dict[str, Any]]) -> None:
        """Use the saved drawing of each picture (the newest, if a picture has several)."""
        chosen: dict[int, dict[str, Any]] = {}
        for c in existing:
            i = c.get("picture")
            if i is None or c.get("problem") or not 0 <= i < len(tiles):
                continue
            if i not in chosen or (Path(c["path"]).stat().st_mtime
                                   > Path(chosen[i]["path"]).stat().st_mtime):
                chosen[i] = c
        if not chosen:
            raise WizardError("Draw and save at least one camera first.")
        names = [c["sensor"] for c in chosen.values()]
        if len(set(names)) != len(names):
            raise WizardError("Two pictures have drawings with the same camera name; rename one.")
        with self._lock:
            self.state["cameras"] = [
                {"picture": i, "sensor": c["sensor"], "site": c["site"], "config": c["path"],
                 "tile": tiles[i]} for i, c in sorted(chosen.items())]
            store = self.state["store"]
            if not store["code"]:
                store["code"] = next(iter(chosen.values()))["site"]
            if not store["name"]:
                store["name"] = self._stores().get(store["code"], "")
            self._save()

    def set_direction(self, direction: str) -> None:
        if direction not in CHOICES:
            raise WizardError("Choose Traffic In, Traffic Out or both.")
        with self._lock:
            self.state["direction"] = direction
            self._save()

    def set_sensor(self, values: dict[str, int | None]) -> None:
        for d, v in values.items():
            if d not in DIRECTIONS:
                raise WizardError(f"unknown direction {d!r}")
            if v is not None and v < 0:
                raise WizardError("A count cannot be negative.")
        with self._lock:
            for d, v in values.items():
                if v is None:
                    self.state["sensor"].pop(d, None)
                else:
                    self.state["sensor"][d] = int(v)
            self._save()

    def set_model(self, model: str) -> None:
        if model not in MODELS:
            raise WizardError(f"unknown detector {model!r}")
        with self._lock:
            self.state["model"] = model
            self._save()

    def set_store(self, name: str | None = None, code: str | None = None,
                  location: str | None = None, report_date: str | None = None,
                  operator: str | None = None) -> None:
        with self._lock:
            store = self.state["store"]
            for key, value in (("name", name), ("code", code), ("location", location),
                               ("report_date", report_date), ("operator", operator)):
                if value is not None:
                    store[key] = value.strip()
            if store["name"] and store["code"]:  # remembered for the next video of this store
                stores = self._stores()
                stores[store["code"]] = store["name"]
                write_json_atomic(self.stores_path, stores)
            self._save()

    # ---- manual counting ---------------------------------------------------------------

    def manual(self) -> bool:
        return self.state.get("mode") == "manual"

    def set_mode(self, mode: str) -> None:
        if mode not in ("auto", "manual"):
            raise WizardError("Choose automatic or manual counting.")
        with self._lock:
            self.state["mode"] = mode
            self._save()

    def set_marks(self, marks: bool) -> None:
        with self._lock:
            self.state["marks"] = bool(marks)
            self._save()

    def set_named_cameras(self, existing: list[dict[str, Any]], tiles: list[dict[str, Any]],
                          chosen: list[dict[str, Any]]) -> None:
        """Cameras of footage that shows the sensor's line: a name per picture, no drawing
        needed. A saved drawing of the picture, if any, is still used to show the line."""
        drawn = {c["picture"]: c for c in existing
                 if c.get("picture") is not None and not c.get("problem")}
        cams: list[dict[str, Any]] = []
        for ch in chosen:
            if not ch.get("include", True):
                continue
            i, name = int(ch["picture"]), str(ch.get("name", "")).strip()
            if not 0 <= i < len(tiles):
                raise WizardError(f"There is no picture {i + 1}.")
            if not name:
                raise WizardError(f"Name the camera in picture {i + 1}.")
            d = drawn.get(i)
            cams.append({"picture": i, "sensor": name, "site": (d or {}).get("site", ""),
                         "config": d["path"] if d else None, "tile": tiles[i]})
        if not cams:
            raise WizardError("Choose at least one camera to count.")
        names = [c["sensor"] for c in cams]
        if len(set(names)) != len(names):
            raise WizardError("Two cameras have the same name.")
        with self._lock:
            self.state["cameras"] = cams
            store = self.state["store"]
            if not store["code"]:
                first = names[0]
                store["code"] = next((c["site"] for c in cams if c["site"]), "") or (
                    first.rsplit("-", 1)[0] if first.count("-") >= 2 else first)
            if not store["name"]:
                store["name"] = self._stores().get(store["code"], "")
            self._save()

    def manual_add(self, sensor: str, t: float, direction: str) -> dict[str, Any]:
        self._camera(sensor)
        if direction not in self.dirs():
            raise WizardError(f"This count is for {' and '.join(LABELS[d] for d in self.dirs())}.")
        if not 0.0 <= t <= float(self.state["duration_s"]) + 1.0:
            raise WizardError(f"{t:.2f} s is outside the video")
        with self._lock:
            m = self.state["manual"]
            c = {"id": m["next_id"], "camera": sensor, "t": round(float(t), 2),
                 "direction": direction, "at": _now()}
            m["next_id"] += 1
            m["counts"].append(c)
            m["done"] = False
            self._save()
            return c

    def manual_delete(self, count_id: int) -> None:
        with self._lock:
            m = self.state["manual"]
            kept = [c for c in m["counts"] if c["id"] != count_id]
            if len(kept) == len(m["counts"]):
                raise WizardError("There is no such count.")
            m["counts"], m["done"] = kept, False
            self._save()

    def manual_undo(self, sensor: str) -> dict[str, Any] | None:
        """Remove the count most recently added on this camera."""
        with self._lock:
            mine = [c for c in self.state["manual"]["counts"] if c["camera"] == sensor]
            if not mine:
                return None
            last = max(mine, key=lambda c: c["id"])
            self.manual_delete(last["id"])
            return dict(last)

    def manual_watched(self, sensor: str, start: float, end: float) -> None:
        self._camera(sensor)
        dur = float(self.state["duration_s"])
        a, b = max(0.0, min(start, end)), min(dur, max(start, end))
        with self._lock:
            m = self.state["manual"]
            if b > a:
                m["watched"][sensor] = merge_ranges([*m["watched"].get(sensor, []), [a, b]])
            m["positions"][sensor] = round(min(dur, max(0.0, end)), 2)
            self._save()

    def manual_position(self, sensor: str, t: float) -> None:
        self._camera(sensor)
        with self._lock:
            self.state["manual"]["positions"][sensor] = round(
                min(float(self.state["duration_s"]), max(0.0, t)), 2)
            self._save()

    def manual_done(self, done: bool = True) -> None:
        with self._lock:
            self.state["manual"]["done"] = bool(done)
            self._save()

    def manual_summary(self) -> dict[str, Any]:
        dur = float(self.state["duration_s"])
        m = self.state["manual"]
        cams = []
        for cam in self.state["cameras"]:
            key = cam["sensor"]
            mine = [c for c in m["counts"] if c["camera"] == key]
            watched = m["watched"].get(key, [])
            seen = sum(b - a for a, b in watched)
            cams.append({"sensor": key, "picture": cam["picture"],
                         "in": sum(1 for c in mine if c["direction"] == "in"),
                         "out": sum(1 for c in mine if c["direction"] == "out"),
                         "watched": watched,
                         "watched_pct": round(min(100.0, 100.0 * seen / dur), 1) if dur else 0.0,
                         "unwatched": unwatched_ranges(watched, dur)})
        return {"cameras": cams, "counts": m["counts"], "positions": m["positions"],
                "done": m["done"]}

    # ---- learning examples -------------------------------------------------------------

    def save_examples(self, root: Path, wait: bool = False) -> None:
        """Save every counted crossing (and some moments with nobody crossing) as frames
        plus a description, for measuring and retraining the automatic counter."""
        from .examples import export_examples

        with self._lock:
            self.state["examples"] = {"status": "saving", "dir": str(root), "started_at": _now()}
            self._save()

        def work() -> None:
            try:
                result = {"status": "saved", **export_examples(self, root)}
            except (OSError, ValueError) as exc:
                result = {"status": "failed", "error": str(exc)}
            with self._lock:
                self.state["examples"] = {**result, "dir": str(root), "finished_at": _now()}
                self._save()

        if wait:
            work()
        else:
            threading.Thread(target=work, daemon=True).start()

    # ---- counting ----------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self.manual():
                raise WizardError("This is a manual count: there is nothing to run.")
            if self._job is not None and self._job.running:
                raise WizardError("The count is already running.")
            if not self.state["cameras"]:
                raise WizardError("Draw the cameras first.")
            if not self.dirs():
                raise WizardError("Choose Traffic In or Traffic Out first.")
            commands = self._commands(self)
            self.state["job"] = {"status": "running", "started_at": _now(), "finished_at": None,
                                 "error": None, "log": []}
            self.state.update(answers={}, added=[], watched=[], watch=None, report=None)
            self._save()
            self._job = _Job(commands, Progress(cameras=len(self.state["cameras"])),
                             self._finished)

    def _finished(self, status: str, error: str | None, log: list[str]) -> None:
        with self._lock:
            self.state["job"].update(status=status, error=error, finished_at=_now(), log=log)
            self._save()

    def stop(self) -> None:
        if self._job is not None:
            self._job.stop()

    def job_status(self) -> dict[str, Any]:
        job = self.state["job"]
        out: dict[str, Any] = {"status": job["status"], "error": job["error"],
                               "log": job["log"][-15:], "pct": 0.0, "message": ""}
        live = self._job
        if live is not None and live.running:
            p = live.progress
            elapsed = time.monotonic() - live.started
            pct = p.overall()
            out.update(status="running", stage=p.stage, stage_pct=round(p.stage_pct, 1),
                       message=p.message, camera=p.camera, pct=round(pct, 1),
                       elapsed_s=round(elapsed),
                       # the gate is far quicker than detection, so estimate only once
                       # detection is under way
                       eta_s=(round(elapsed * (100 - pct) / pct)
                              if p.stage == "detect" and pct > 8 else None),
                       log=p.log[-15:])
        elif job["status"] == "done":
            out.update(pct=100.0, message="Finished", results=self.results())
        return out

    def results(self) -> list[dict[str, Any]]:
        out = []
        for cam in self.state["cameras"]:
            s = self._read(cam, "candidates").get("summary", {})
            out.append({"sensor": cam["sensor"], "in": s.get("in", 0), "out": s.get("out", 0),
                        "likely_missed": s.get("likely_missed_crossings", 0)})
        return out

    # ---- checking ----------------------------------------------------------------------

    def geometry(self) -> dict[str, dict[str, Any]]:
        """Each camera's picture, counting line and mask zone, in video pixels."""
        out: dict[str, dict[str, Any]] = {}
        for cam in self.state["cameras"]:
            t = cam["tile"]
            if not cam.get("config"):  # footage with the sensor's own line: nothing drawn
                out[cam["sensor"]] = {"picture": cam["picture"], "tile": t, "line": None,
                                      "mask": None}
                continue
            g = bind(load_config(cam["config"]), t["x1"] - t["x0"], t["y1"] - t["y0"],
                     (t["x0"], t["y0"]))
            out[cam["sensor"]] = {"picture": cam["picture"], "tile": t, "line": _pts(g.line),
                                  "mask": _pts(g.mask_zone) if g.mask_zone is not None else None}
        return out

    def check_items(self) -> list[dict[str, Any]]:
        """Every counted crossing, then every possible miss, in the chosen directions."""
        if self.state["job"]["status"] != "done":
            return []
        dirs = self.dirs()
        dur = float(self.state["duration_s"])

        def clip(t0: float, t1: float | None = None) -> list[float]:
            return [round(max(0.0, t0 - CLIP_BEFORE_S), 2),
                    round(min(dur, (t0 if t1 is None else t1) + CLIP_AFTER_S), 2)]

        items: list[dict[str, Any]] = []
        for cam in self.state["cameras"]:
            base = {"camera": cam["sensor"], "picture": cam["picture"]}
            for c in self._read(cam, "candidates").get("candidates", []):
                if c["direction"] in dirs:
                    t = float(c["t_seconds"])
                    items.append({**base, "id": c["id"], "kind": "counted", "why": "detected",
                                  "t": t, "direction": c["direction"], "clip": clip(t),
                                  "point": c.get("crossing_xy"), "path": c.get("path")})
            for d in self._read(cam, "discarded").get("discarded", []):
                first = (d.get("crossings") or [{}])[0]
                if d.get("reason") in POSSIBLE_REASONS and first.get("direction") in dirs:
                    t = float(first["t"])
                    items.append({**base, "id": d["id"], "kind": "possible", "why": d["reason"],
                                  "t": t, "direction": first["direction"], "clip": clip(t, t + 1.5),
                                  "point": _near(d.get("path"), t), "path": d.get("path")})
            for u in self._read(cam, "unexplained").get("unexplained", []):
                if u.get("kind") == MISS_KIND and u.get("direction_guess") in dirs:
                    items.append({**base, "id": u["id"], "kind": "possible", "why": "lost",
                                  "t": float(u["t_seconds"]), "direction": u["direction_guess"],
                                  "clip": clip(float(u["start_s"]), float(u["end_s"])),
                                  "point": None, "path": None})
        items.sort(key=lambda i: (i["kind"] != "counted",
                                  prompt_priority(i["why"]) if i["kind"] == "possible" else 0,
                                  i["t"], i["picture"]))
        mark_twins(items)
        return items

    def watch_ranges(self) -> list[dict[str, Any]]:
        """Movement near the line that produced no count and no likely miss."""
        if self.state["job"]["status"] != "done":
            return []
        out = []
        for cam in self.state["cameras"]:
            for u in self._read(cam, "unexplained").get("unexplained", []):
                if u.get("kind") != MISS_KIND:
                    out.append({"id": u["id"], "camera": cam["sensor"], "picture": cam["picture"],
                                "start": float(u["start_s"]), "end": float(u["end_s"])})
        out.sort(key=lambda r: (r["start"], r["picture"]))
        return out

    def answer(self, item_id: str, answer: str | None) -> None:
        if answer not in ("yes", "no", None):
            raise WizardError("answer yes or no")
        if item_id not in {i["id"] for i in self.check_items()}:
            raise WizardError(f"there is no crossing {item_id} to check")
        with self._lock:
            if answer is None:
                self.state["answers"].pop(item_id, None)
            else:
                self.state["answers"][item_id] = answer
            self._save()

    def add(self, sensor: str, t: float, direction: str, range_id: str | None = None) -> None:
        self._camera(sensor)
        if direction not in self.dirs():
            raise WizardError(f"This count is for {' and '.join(LABELS[d] for d in self.dirs())}.")
        if not 0.0 <= t <= float(self.state["duration_s"]) + 1.0:
            raise WizardError(f"{t:.2f} s is outside the video")
        with self._lock:
            self.state["added"].append({"camera": sensor, "t": round(float(t), 2),
                                        "direction": direction, "range": range_id, "at": _now()})
            self._save()

    def remove_added(self, index: int) -> None:
        with self._lock:
            if not 0 <= index < len(self.state["added"]):
                raise WizardError("no such added crossing")
            del self.state["added"][index]
            self._save()

    def set_watch(self, mode: str | None) -> None:
        if mode not in ("doing", "done", "skipped", None):
            raise WizardError("watch mode must be doing, done or skipped")
        with self._lock:
            self.state["watch"] = mode
            self._save()

    def mark_watched(self, range_id: str, done: bool = True) -> None:
        with self._lock:
            watched = [r for r in self.state["watched"] if r != range_id]
            self.state["watched"] = [*watched, range_id] if done else watched
            self._save()

    def counts(self) -> dict[str, Any]:
        dirs = self.dirs()
        if self.manual():
            m = self.state["manual"]
            verified = {d: sum(1 for c in m["counts"] if c["direction"] == d) for d in dirs}
            return {"manual": True, "detected": dict.fromkeys(dirs, 0), "verified": verified,
                    "confirmed": 0, "rejected": 0, "found": 0, "not_found": 0, "added": 0,
                    "unanswered": 0, "items": 0, "watch_ranges": 0, "watch_s": 0.0,
                    "watch_done": 0,
                    "watched_pct": {c["sensor"]: c["watched_pct"]
                                    for c in self.manual_summary()["cameras"]},
                    "accuracy": {d: sensor_accuracy(self.state["sensor"].get(d), verified[d])
                                 for d in dirs},
                    "checked": bool(m["done"])}
        answers = self.state["answers"]
        c: dict[str, Any] = {"detected": dict.fromkeys(dirs, 0), "verified": dict.fromkeys(dirs, 0),
                             "confirmed": 0, "rejected": 0, "found": 0, "not_found": 0,
                             "added": 0, "unanswered": 0}
        items = self.check_items()
        for it in items:
            if it["kind"] == "counted":
                c["detected"][it["direction"]] += 1
            a = answers.get(it["id"])
            if a is None:
                c["unanswered"] += 1
                continue
            if a == "yes":
                c["verified"][it["direction"]] += 1
            if it["kind"] == "counted":
                c["confirmed" if a == "yes" else "rejected"] += 1
            else:
                c["found" if a == "yes" else "not_found"] += 1
        for a in self.state["added"]:
            if a["direction"] in dirs:
                c["verified"][a["direction"]] += 1
                c["added"] += 1
        ranges = self.watch_ranges()
        c["items"] = len(items)
        c["watch_ranges"] = len(ranges)
        c["watch_s"] = round(sum(r["end"] - r["start"] for r in ranges), 1)
        c["watch_done"] = sum(1 for r in ranges if r["id"] in self.state["watched"])
        c["accuracy"] = {d: sensor_accuracy(self.state["sensor"].get(d), c["verified"][d])
                         for d in dirs}
        c["checked"] = (self.state["job"]["status"] == "done" and c["unanswered"] == 0
                        and (self.state["watch"] in ("done", "skipped") or not ranges))
        return c

    def verified_rows(self) -> list[dict[str, Any]]:
        if self.manual():
            rows = [{"t": c["t"], "camera": c["camera"],
                     "picture": self._camera(c["camera"])["picture"], "direction": c["direction"],
                     "point": None, "found": "Counted by hand"}
                    for c in self.state["manual"]["counts"] if c["direction"] in self.dirs()]
            rows.sort(key=lambda r: (r["t"], r["picture"]))
            return rows
        answers = self.state["answers"]
        rows = [{"t": it["t"], "camera": it["camera"], "picture": it["picture"],
                 "direction": it["direction"], "point": it["point"],
                 "found": FOUND.get(it["why"], "Rejected by rule, restored")}
                for it in self.check_items() if answers.get(it["id"]) == "yes"]
        for a in self.state["added"]:
            if a["direction"] in self.dirs():
                rows.append({"t": a["t"], "camera": a["camera"],
                             "picture": self._camera(a["camera"])["picture"],
                             "direction": a["direction"], "point": None, "found": FOUND["added"]})
        rows.sort(key=lambda r: (r["t"], r["picture"]))
        return rows

    def public(self) -> dict[str, Any]:
        with self._lock:
            return {**self.state, "dirs": self.dirs(), "counts": self.counts(),
                    "run_dir": str(self.run_dir)}

    # ---- pictures ----------------------------------------------------------------------

    def _frame(self, t: float) -> Image:
        t = max(0.0, min(t, float(self.state["duration_s"]) - 0.2))
        frames = vid.grab_frames_at(self.video, [t])
        if not frames:
            raise WizardError(f"could not read the video at {t:.1f} s")
        return frames[0].image

    @staticmethod
    def _draw_geometry(img: Image, g: dict[str, Any]) -> None:
        if not g["line"]:
            return
        if g["mask"]:
            shade = img.copy()
            cv2.fillPoly(shade, [np.asarray(g["mask"], dtype=np.int32).reshape(-1, 1, 2)],
                         (190, 190, 190))
            cv2.addWeighted(shade, 0.35, img, 0.65, 0, dst=img)
        cv2.polylines(img, [np.asarray(g["line"], dtype=np.int32).reshape(-1, 1, 2)], False,
                      (235, 206, 30), 2, cv2.LINE_AA)

    @staticmethod
    def _crop(img: Image, tile: dict[str, Any]) -> Image:
        return img[tile["y0"]:tile["y1"], tile["x0"]:tile["x1"]]

    def preview_jpeg(self, picture: int, t: float) -> bytes:
        """One camera's picture at t with its line drawn: the live view while counting."""
        cam = next((c for c in self.state["cameras"] if c["picture"] == picture), None)
        if cam is None:
            raise WizardError(f"no camera on picture {picture}")
        img = self._frame(t)
        self._draw_geometry(img, self.geometry()[cam["sensor"]])
        ok, buf = cv2.imencode(".jpg", self._crop(img, cam["tile"]), [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            raise WizardError("could not encode the picture")
        return bytes(buf.tobytes())

    def busiest(self) -> tuple[dict[str, Any], float, list[Any]]:
        """The camera and moment with the most people detected near the line."""
        best: tuple[int, dict[str, Any], float, list[Any]] | None = None
        for cam in self.state["cameras"]:
            pkl = camera_dir(self.run_dir, cam["sensor"]) / "detections.pkl"
            if not pkl.is_file():
                continue
            try:  # written by detect.py --record during this count
                rec = pickle.loads(pkl.read_bytes())
            except (pickle.UnpicklingError, EOFError, AttributeError, ImportError):
                continue
            for t, dets in sorted(rec.get("frames", {}).items()):
                people = distinct_people(list(dets))
                if best is None or len(people) > best[0]:
                    best = (len(people), cam, float(t), people)
        if best is None:  # no recording: the proposal with most people around it
            for cam in self.state["cameras"]:
                for c in self._read(cam, "candidates").get("candidates", []):
                    n = int(c.get("concurrent_tracks", 0))
                    if best is None or n > best[0]:
                        best = (n, cam, float(c["t_seconds"]), [])
        if best is None:
            return self.state["cameras"][0], float(self.state["duration_s"]) / 2, []
        return best[1], best[2], best[3]

    def _render_manual_frame(self) -> tuple[Path, str]:
        """For a manual count: the moment with the most counted crossings close together."""
        counts = [c for c in self.state["manual"]["counts"] if c["direction"] in self.dirs()]

        def near(c: dict[str, Any]) -> int:
            return sum(1 for o in counts
                       if o["camera"] == c["camera"] and abs(o["t"] - c["t"]) <= 5.0)

        if counts:
            best = max(counts, key=lambda c: (near(c), -c["t"]))
            cam, t, n = self._camera(best["camera"]), float(best["t"]), near(best)
            note = f"{n} crossing{'s' if n != 1 else ''} within 10 s"
        else:
            cam, t, note = self.state["cameras"][0], float(self.state["duration_s"]) / 2, ""
        img = self._frame(t)
        self._draw_geometry(img, self.geometry()[cam["sensor"]])
        crop = cv2.resize(self._crop(img, cam["tile"]), None, fx=2, fy=2,
                          interpolation=cv2.INTER_CUBIC)
        path = self.dir / "busy_frame.jpg"
        cv2.imwrite(str(path), crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
        return path, " · ".join(x for x in (cam["sensor"], self.clock(t), note) if x)

    def render_busy_frame(self) -> tuple[Path, str]:
        if self.manual():
            return self._render_manual_frame()
        cam, t, dets = self.busiest()
        img = self._frame(t)
        self._draw_geometry(img, self.geometry()[cam["sensor"]])
        for d in dets:
            quad = tuple(getattr(d, "quad", ()) or ())
            if len(quad) == 4:
                cv2.polylines(img, [np.asarray(quad, dtype=np.int32).reshape(-1, 1, 2)], True,
                              (60, 220, 60), 2, cv2.LINE_AA)
            else:
                x0, y0, x1, y1 = (int(v) for v in d.bbox)
                cv2.rectangle(img, (x0, y0), (x1, y1), (60, 220, 60), 2)
        crop = cv2.resize(self._crop(img, cam["tile"]), None, fx=2, fy=2,
                          interpolation=cv2.INTER_CUBIC)
        path = self.dir / "busy_frame.jpg"
        cv2.imwrite(str(path), crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
        people = f"{len(dets)} people detected" if dets else "busiest moment"
        return path, f"{cam['sensor']} · {self.clock(t)} · {people}"

    def render_thumbnails(self, rows: list[dict[str, Any]]) -> list[dict[str, str]]:
        """A close-up of each verified crossing, at the moment it happened."""
        out_dir = self.dir / "thumbs"
        out_dir.mkdir(parents=True, exist_ok=True)
        for old in out_dir.glob("*.jpg"):
            old.unlink()
        if not rows:
            return []
        geometry = self.geometry()
        times = sorted({round(float(r["t"]), 2) for r in rows})
        frames = {t: f.image for t, f in zip(times, vid.grab_frames_at(self.video, times),
                                             strict=False)}
        thumbs: list[dict[str, str]] = []
        for n, r in enumerate(rows, 1):
            frame = frames.get(round(float(r["t"]), 2))
            if frame is None:
                continue
            img = frame.copy()
            g = geometry[r["camera"]]
            tile = g["tile"]
            self._draw_geometry(img, g)
            if self.manual() or not r["point"] and not g["line"]:
                # where a hand-counted person crossed is not recorded: the whole picture
                crop = cv2.resize(self._crop(img, tile), (480, 360), interpolation=cv2.INTER_AREA)
            else:
                cx, cy = r["point"] if r["point"] else np.asarray(g["line"]).mean(axis=0)
                colour = (60, 220, 60) if r["direction"] == "in" else (0, 140, 255)
                if r["point"]:
                    cv2.circle(img, (int(cx), int(cy)), 16, colour, 3, cv2.LINE_AA)
                half = int(min(130, (tile["x1"] - tile["x0"]) // 2,
                               (tile["y1"] - tile["y0"]) // 2))
                x0 = int(min(max(cx - half, tile["x0"]), tile["x1"] - 2 * half))
                y0 = int(min(max(cy - half, tile["y0"]), tile["y1"] - 2 * half))
                crop = cv2.resize(img[y0:y0 + 2 * half, x0:x0 + 2 * half], (400, 400),
                                  interpolation=cv2.INTER_CUBIC)
            p = out_dir / f"{n:03d}.jpg"
            cv2.imwrite(str(p), crop, [cv2.IMWRITE_JPEG_QUALITY, 88])
            thumbs.append({"path": str(p), "label": f"{n} · {r['direction'].upper()} "
                                                    f"{self.clock(r['t'])} · {r['camera']}"})
        return thumbs

    # ---- the report --------------------------------------------------------------------

    def _period(self) -> tuple[datetime | None, datetime | None]:
        st = self.state
        start = datetime.fromisoformat(st["clock_start"]) if st["clock_start"] else None
        if st.get("clock_end"):
            end: datetime | None = datetime.fromisoformat(st["clock_end"])
        else:
            end = start + timedelta(seconds=float(st["duration_s"])) if start else None
        return start, end

    def report_data(self, c: dict[str, Any], rows: list[dict[str, Any]], frame: Path,
                    caption: str, thumbs: list[dict[str, str]]) -> dict[str, Any]:
        st, store = self.state, self.state["store"]
        start, end = self._period()
        cams = ", ".join(cam["sensor"] for cam in st["cameras"])
        if st["watch"] == "done":
            watched = (f"The {c['watch_ranges']} stretches of movement the tool did not count "
                       f"({_dur(c['watch_s'])}) were also watched; {c['added']} crossing(s) were "
                       f"added.")
        elif c["watch_ranges"]:
            watched = ("Stretches of movement the tool did not count were not watched, so "
                       "anyone the tool never detected is not in the verified count.")
        else:
            watched = "There was no movement near the line that the tool left unexplained."
        method = [
            f"Footage: {st['filename']} ({_dur(float(st['duration_s']))}), cameras {cams}.",
            ("People were detected and tracked automatically, and crossings of each camera's "
             "counting line found with the camera's rule (line and mask zone)."),
            (f"A person checked every crossing the tool found: {c['confirmed']} confirmed, "
             f"{c['rejected']} rejected. Of {c['found'] + c['not_found']} possible misses it "
             f"listed, {c['found']} were real."),
            watched,
            ("System count: RetailNext, same cameras and period. Accuracy = 100% minus the "
             "system's error as a share of the verified count."),
        ]
        if self.manual():
            summary = self.manual_summary()["cameras"]
            shares = ", ".join(f"{c['sensor']} {c['watched_pct']:.0f}%" for c in summary)
            method = [
                method[0],
                ("Counted by hand: a person watched each camera and pressed a key at every "
                 "crossing of the counting line. No automatic detection was used."),
                f"Share of the footage watched: {shares}.",
                method[-1],
            ]
            if any(c["watched_pct"] < 99 for c in summary):
                method.insert(3, "Parts of the footage were not watched, so the count may be low.")
        return {
            "count_label": "MANUAL COUNT" if self.manual() else "VERIFIED COUNT",
            "frames_title": ("VALIDATION FRAMES — BUSIEST MOMENT" if self.manual()
                             else "VALIDATION FRAMES — AUTOMATED DETECTION OVERLAY"),
            "store_name": store["name"], "store_code": store["code"],
            "location": store["location"] or "Entrance",
            "report_date": store["report_date"] or datetime.now().astimezone().strftime("%d/%m/%Y"),
            "captured_date": start.strftime("%d/%m/%Y") if start else "",
            "time_range": f"{start:%H:%M}-{end:%H:%M}" if start and end else "",
            "directions": [{"key": d, "label": LABELS[d], "verified": c["verified"][d],
                            "system": st["sensor"][d],
                            "accuracy": c["accuracy"][d]["accuracy_pct"]} for d in self.dirs()],
            "frame": str(frame), "frame_caption": caption,
            "crossings": [{"n": n, "time": self.clock(r["t"]), "camera": r["camera"],
                           "direction": LABELS[r["direction"]], "found": r["found"]}
                          for n, r in enumerate(rows, 1)],
            "thumbs": thumbs, "method": method,
        }

    def make_report(self, logo: Path | None = None) -> Path:
        with self._lock:
            c = self.counts()
            if self.manual():
                if not c["checked"]:
                    raise WizardError("Finish counting first.")
            else:
                if self.state["job"]["status"] != "done":
                    raise WizardError("Run the count first.")
                if c["unanswered"]:
                    raise WizardError(f"{c['unanswered']} crossing(s) are still to check.")
                if not c["checked"]:
                    raise WizardError("Watch or skip the movement the tool did not count first.")
            if any(self.state["sensor"].get(d) is None for d in self.dirs()):
                raise WizardError("Enter RetailNext's count first.")
            store = self.state["store"]
            if not store["name"] or not store["code"]:
                raise WizardError("Enter the store name and code.")
            rows = self.verified_rows()
            frame, caption = self.render_busy_frame()
            thumbs = self.render_thumbnails(rows)
            data = self.report_data(c, rows, frame, caption, thumbs)
            start, end = self._period()
            when = f"{start:%Y-%m-%d %H%M}-{end:%H%M}" if start and end else "report"
            words = f"{store['code']} {store['name']} camera validation {when}"
            out = self.dir / (re.sub(r"[^A-Za-z0-9 ._-]+", "-", words).strip() + ".pptx")
            build_report(data, out, logo)
            self.state["report"] = {"path": str(out), "made_at": _now()}
            self._save()
            return out
