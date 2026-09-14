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
import shutil
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

from . import __copyright__, paths
from . import video as vid
from .candidates import MISS_KIND
from .config import bind, load_config
from .export import sensor_accuracy
from .gating import camera_dir
from .manual import intervals_for, merge_ranges, unwatched_ranges
from .report_pptx import build_report
from .util import default_run_dir, fmt_hms, same_store, write_json_atomic
from .version import app_version

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
MIN_WATCHED_PCT = 99.0  # a hand count that watched less of a camera's footage is incomplete
MAX_GROUP = 9  # people one answer can count, when a group crosses together
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


def accuracy_range(sensor: int | None, verified: int, unsure: int) -> list[float] | None:
    """The system's accuracy whichever way the unsure crossings go: [lowest, highest]."""
    vals = [a for v in range(verified, verified + unsure + 1)
            if (a := sensor_accuracy(sensor, v)["accuracy_pct"]) is not None]
    return [min(vals), max(vals)] if vals else None


def row_numbers(rows: list[dict[str, Any]]) -> list[str]:
    """The report's numbering, one number per person: a group of 3 after two people is "3–5"."""
    out, k = [], 1
    for r in rows:
        n = int(r.get("people", 1))
        out.append(str(k) if n == 1 else f"{k}–{k + n - 1}")
        k += n
    return out


def _found(why: str, people: int) -> str:
    text = FOUND.get(why, "Rejected by rule, restored")
    return f"{text} (group of {people})" if people > 1 else text


def review_items(camera: str, picture: int, candidates: dict[str, Any], discarded: dict[str, Any],
                 unexplained: dict[str, Any], dirs: list[str], duration: float
                 ) -> list[dict[str, Any]]:
    """What one camera's count asks a person to check: every counted crossing, and every
    possible miss (rule rejections that are often real, tracks lost at the line)."""
    def clip(t0: float, t1: float | None = None) -> list[float]:
        return [round(max(0.0, t0 - CLIP_BEFORE_S), 2),
                round(min(duration, (t0 if t1 is None else t1) + CLIP_AFTER_S), 2)]

    base = {"camera": camera, "picture": picture}
    items: list[dict[str, Any]] = []
    for c in candidates.get("candidates", []):
        if c["direction"] in dirs:
            t = float(c["t_seconds"])
            items.append({**base, "id": c["id"], "kind": "counted", "why": "detected",
                          "t": t, "direction": c["direction"], "clip": clip(t),
                          "point": c.get("crossing_xy"), "path": c.get("path")})
    for d in discarded.get("discarded", []):
        first = (d.get("crossings") or [{}])[0]
        if d.get("reason") in POSSIBLE_REASONS and first.get("direction") in dirs:
            t = float(first["t"])
            items.append({**base, "id": d["id"], "kind": "possible", "why": d["reason"],
                          "t": t, "direction": first["direction"], "clip": clip(t, t + 1.5),
                          "point": _near(d.get("path"), t), "path": d.get("path")})
    for u in unexplained.get("unexplained", []):
        if u.get("kind") == MISS_KIND and u.get("direction_guess") in dirs:
            items.append({**base, "id": u["id"], "kind": "possible", "why": "lost",
                          "t": float(u["t_seconds"]), "direction": u["direction_guess"],
                          "clip": clip(float(u["start_s"]), float(u["end_s"])),
                          "point": None, "path": None})
    return items


def watch_stretches(camera: str, picture: int, unexplained: dict[str, Any]) -> list[dict[str, Any]]:
    """Movement near the line that gave no count and no possible miss: a person the tool
    never detected there is found only by watching it."""
    return [{"id": u["id"], "camera": camera, "picture": picture,
             "start": float(u["start_s"]), "end": float(u["end_s"])}
            for u in unexplained.get("unexplained", []) if u.get("kind") != MISS_KIND]


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
        for key, value in (("mode", None), ("marks", None), ("examples", None), ("people", {}),
                           ("decisions", []), ("sensor_intervals", {}), ("sensor_cameras", {}),
                           ("sensor_source", None),
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

    def set_sensor(self, values: dict[str, int | None] | None = None,
                   intervals: dict[str, dict[str, int | None]] | None = None,
                   cameras: dict[str, dict[str, int | None]] | None = None) -> None:
        """RetailNext's numbers: a total per direction, or one per 15-minute interval (the
        total is then their sum), and optionally each camera's own number."""
        def check(per: dict[str, int | None]) -> dict[str, int]:
            out: dict[str, int] = {}
            for d, v in per.items():
                if d not in DIRECTIONS:
                    raise WizardError(f"unknown direction {d!r}")
                if v is not None and v < 0:
                    raise WizardError("A count cannot be negative.")
                if v is not None:
                    out[d] = int(v)
            return out

        known = {i["key"]: i["label"] for i in self.intervals()}
        ivs: dict[str, dict[str, int]] = {}
        for key, per in (intervals or {}).items():
            if key not in known:
                raise WizardError(f"{key} is not one of this footage's 15-minute intervals")
            ivs[key] = check(per)
        cams: dict[str, dict[str, int]] = {}
        for cam, per in (cameras or {}).items():
            self._camera(cam)
            if got := check(per):
                cams[cam] = got
        totals = check(values or {})
        with self._lock:
            if intervals is not None:
                missing = [label for key, label in known.items()
                           if any(d not in ivs.get(key, {}) for d in self.dirs())]
                if missing:
                    raise WizardError(f"Enter RetailNext's number for {missing[0]}.")
                self.state["sensor_intervals"] = ivs
                totals = {d: sum(per[d] for per in ivs.values()) for d in self.dirs()}
            elif values is not None:
                self.state["sensor_intervals"] = {}  # a plain total replaces the intervals
            for d, v in (values or {}).items():
                if v is None:
                    self.state["sensor"].pop(d, None)
            self.state["sensor"].update(totals)
            if cameras is not None:
                self.state["sensor_cameras"] = cams
            src = self.state.get("sensor_source")
            if src and src.get("numbers") != self._sensor_numbers():
                self.state["sensor_source"] = None  # changed by hand: no longer RetailNext's own
            self._save()

    def _sensor_numbers(self) -> dict[str, Any]:
        return {"sensor": dict(self.state["sensor"]),
                "intervals": dict(self.state["sensor_intervals"]),
                "cameras": dict(self.state["sensor_cameras"])}

    def use_retailnext(self, got: dict[str, Any]) -> list[str]:
        """RetailNext's numbers from its API (retailnext.camera_counts): the cameras' sum
        per 15-minute interval, and each camera's own. Returns warnings for intervals
        RetailNext marked incomplete or imputed, which are not full counts."""
        keys = [i["key"] for i in self.intervals()]
        if keys == ["all"]:
            raise WizardError("The video's name has no clock time, so RetailNext's numbers "
                              "cannot be looked up.")
        dirs = self.dirs()
        per_iv = {k: dict.fromkeys(dirs, 0) for k in keys}
        per_cam: dict[str, dict[str, int]] = {}
        warnings: list[str] = [str(got["note"])] if got.get("note") else []
        # each camera's rows, or the store's total when its entrances are not the cameras
        sources = got["cameras"] or {"": got.get("total") or []}
        for cam, rows in sources.items():
            by_start = {str(r["start"]): r for r in rows}
            label = cam or str(got.get("store") or "the store")
            if cam:
                per_cam[cam] = dict.fromkeys(dirs, 0)
            for k in keys:
                r = by_start.get(k)
                if r is None:
                    raise WizardError(f"RetailNext gave no number for {label} at {k}.")
                if r["validity"] != "complete":
                    warnings.append(f"RetailNext marked {label} {r['start']}–{r['finish']} as "
                                    f"{r['validity']}: its number there is not a full count.")
                for d in dirs:
                    per_iv[k][d] += int(r.get(d) or 0)
                    if cam:
                        per_cam[cam][d] += int(r.get(d) or 0)
        if len(keys) > 1:
            self.set_sensor(intervals={k: dict(v) for k, v in per_iv.items()},
                            cameras={c: dict(v) for c, v in per_cam.items()})
        else:
            self.set_sensor(values=dict(per_iv[keys[0]]),
                            cameras={c: dict(v) for c, v in per_cam.items()})
        with self._lock:
            self.state["sensor_source"] = {
                "source": "RetailNext API", "subscription": got.get("subscription"),
                "store": got.get("store"),
                "cameras": list(got["cameras"]) or ["the store's total"],
                "fetched_at": _now(), "warnings": warnings, "numbers": self._sensor_numbers()}
            self._save()
        return warnings

    def use_download(self, info: dict[str, Any]) -> list[str]:
        """Footage downloaded from RetailNext (retailnext.store_summary): its store is known,
        so it is used, and a camera filed under another store's drawing is corrected: named
        as RetailNext names that picture's camera, its hand counts moved with it, the other
        store's line no longer shown. Returns what was corrected, in words."""
        notes: list[str] = []
        code = str(info.get("code") or "")
        if not code:
            return notes
        full = str(info.get("name") or "")
        with self._lock:
            self.state["retailnext"] = {**(self.state.get("retailnext") or {}), **info}
            store = self.state["store"]
            if store.get("code") != code:
                if store.get("code"):
                    notes.append(f"This footage is RetailNext's store {code}, not {store['code']}.")
                store["code"] = code
                # RetailNext's names carry the code ("392 Perri Cutten Armadale"): the report
                # shows it beside the name already
                store["name"] = (" ".join(full.replace(code, " ").split())
                                 or self._stores().get(code, ""))
            names = [str(n) for n in info.get("cameras") or []]
            for cam in list(self.state["cameras"]):
                site = str(cam.get("site") or "")
                if not site or same_store(site, (code, full)):
                    continue
                old, i = cam["sensor"], int(cam["picture"])
                new = names[i] if i < len(names) else old
                cam.update(site=code, config=None)
                if new != old and all(c["sensor"] != new for c in self.state["cameras"]):
                    self._rename_camera(old, new)
                notes.append(f"Picture {i + 1} was using {old}, a camera of store {site}: it is "
                             f"now {new if new != old else old} of {code}"
                             + (", with its counts." if self.manual() else
                                ". Draw this store's camera and run the count again."))
                self._decide("corrected", picture=i, was=old, now=new, was_store=site, store=code)
            self._save()
        return notes

    def _rename_camera(self, old: str, new: str) -> None:
        """A camera's new name everywhere it is kept: counts, watched time, additions."""
        m = self.state["manual"]
        for c in m.get("counts", []):
            if c["camera"] == old:
                c["camera"] = new
        if old in m.get("watched", {}):
            m["watched"][new] = merge_ranges([*m["watched"].get(new, []), *m["watched"].pop(old)])
        if old in m.get("positions", {}):
            m["positions"].setdefault(new, m["positions"].pop(old))
        for a in self.state["added"]:
            if a["camera"] == old:
                a["camera"] = new
        if old in self.state["sensor_cameras"]:
            self.state["sensor_cameras"][new] = self.state["sensor_cameras"].pop(old)
        for cam in self.state["cameras"]:
            if cam["sensor"] == old:
                cam["sensor"] = new

    def tidy_on_open(self, pictures: int) -> list[str]:
        """Put right what earlier versions left behind, logged: hand counts under a camera's
        earlier name (a rename did not move them) go back to the camera, when the footage has
        one picture and so its counts are that camera's."""
        notes: list[str] = []
        with self._lock:
            m, cams = self.state["manual"], self.state["cameras"]
            names = {c["sensor"] for c in cams}
            left = sorted({c["camera"] for c in m.get("counts", []) if c["camera"] not in names}
                          | {k for k in m.get("watched", {}) if k not in names})
            if pictures == 1 and len(cams) == 1 and len(left) == 1:
                new = cams[0]["sensor"]
                n = sum(1 for c in m["counts"] if c["camera"] == left[0])
                self._rename_camera(left[0], new)
                self._decide("reattached", was=left[0], now=new, counts=n)
                notes.append(f"{n} hand count(s) made when this camera was called {left[0]} "
                             f"count again, as {new}.")
                self._save()
        return notes

    def marked(self) -> bool:
        """A hand count on footage showing RetailNext's own line: no drawing is shown over it."""
        return self.manual() and bool(self.state.get("marks"))

    def period(self) -> tuple[datetime | None, datetime | None]:
        """The footage's clock period, from its name."""
        return self._period()

    def _system_line(self) -> str:
        src = self.state.get("sensor_source")
        sub = f", subscription {src['subscription']}" if src and src.get("subscription") else ""
        how = (f"fetched from RetailNext's API ({src['store']}{sub}: "
               f"{' + '.join(src['cameras'])}) on {str(src['fetched_at'])[:10]}"
               if src else "typed in from RetailNext")
        text = (f"System count: RetailNext, same cameras and period, {how}. Accuracy = 100% "
                f"minus the system's error as a share of the verified count.")
        return text + "".join(f" {w}" for w in (src or {}).get("warnings", []))

    def intervals(self) -> list[dict[str, Any]]:
        """RetailNext's 15-minute intervals this footage overlaps, and how much of each."""
        cs = self.state["clock_start"]
        return intervals_for(datetime.fromisoformat(cs) if cs else None,
                             float(self.state["duration_s"]))

    def comparison(self) -> dict[str, Any]:
        """Verified against RetailNext per 15-minute interval and per camera: the one place
        these are worked out, for the page and the report alike."""
        dirs = self.dirs()
        ivs = self.intervals()
        cs = self.state["clock_start"]
        start = datetime.fromisoformat(cs) if cs else None

        def key_of(t: float) -> str:
            if start is None:
                return str(ivs[0]["key"])
            at = start + timedelta(seconds=t)
            for i in reversed(ivs):
                if datetime.fromisoformat(i["start"]) <= at:
                    return str(i["key"])
            return str(ivs[0]["key"])

        by_iv = {i["key"]: dict.fromkeys(dirs, 0) for i in ivs}
        by_cam = {c["sensor"]: dict.fromkeys(dirs, 0) for c in self.state["cameras"]}
        for r in self.verified_rows():
            if r["direction"] not in dirs:
                continue
            n = int(r.get("people", 1))
            by_iv[key_of(float(r["t"]))][r["direction"]] += n
            if r["camera"] in by_cam:
                by_cam[r["camera"]][r["direction"]] += n

        def against(sensor: dict[str, int] | None, verified: dict[str, int]
                    ) -> dict[str, Any] | None:
            return {d: sensor_accuracy(sensor.get(d), verified[d]) for d in dirs} if sensor else None

        s_iv, s_cam = self.state["sensor_intervals"], self.state["sensor_cameras"]
        intervals = [{**i, "verified": by_iv[i["key"]], "sensor": s_iv.get(i["key"]),
                      "accuracy": against(s_iv.get(i["key"]), by_iv[i["key"]])} for i in ivs]
        cameras = [{"camera": c, "verified": v, "sensor": s_cam.get(c),
                    "accuracy": against(s_cam.get(c), v)} for c, v in by_cam.items()]
        overlap = 0 if self.manual() else sum(
            1 for it in self.check_items() if it.get("twin") and it["twin"]["camera"] != it["camera"])
        diffs = [(abs(a[d]["error"]), i["label"], d, a[d]) for i in intervals
                 if (a := i["accuracy"]) for d in dirs if a[d]["error"] is not None]
        largest = None
        if len(intervals) > 1 and diffs:
            size, label, d, acc = max(diffs, key=lambda x: x[0])
            if size > 0:
                largest = {"interval": label, "direction": d, **acc}
        return {"intervals": intervals, "cameras": cameras, "overlap": overlap, "largest": largest}

    def _breakdown(self) -> dict[str, Any]:
        """The report's page of 15-minute intervals and cameras, from comparison()."""
        comp = self.comparison()
        ivs = comp["intervals"] if len(comp["intervals"]) > 1 else []
        # cameras only with something to set them against: their own RetailNext numbers,
        # or the page of intervals they share
        cams = (comp["cameras"] if len(comp["cameras"]) > 1
                and (ivs or any(c["sensor"] for c in comp["cameras"])) else [])
        notes = []
        if part := [i["label"] for i in ivs if i["full"] is False]:
            notes.append(f"Only partly in the footage: {', '.join(part)}. RetailNext's numbers "
                         f"cover whole intervals, so these do not compare like for like.")
        if (big := comp["largest"]) and ivs:
            notes.append(f"Largest difference: {big['interval']}, {LABELS[big['direction']]}: "
                         f"RetailNext {big['sensor']} against {big['verified']} verified.")
        if cams and comp["overlap"]:
            notes.append(f"{comp['overlap']} crossing(s) were seen on two cameras within "
                         f"{TWIN_WINDOW_S:g} s: the cameras overlap, so each camera's number "
                         f"can differ from RetailNext's even when the total agrees. Compare "
                         f"the combined total.")
        return {"dirs": [{"key": d, "label": LABELS[d]} for d in self.dirs()],
                "intervals": [{"label": i["label"], "partial": i["full"] is False,
                               "verified": i["verified"], "sensor": i["sensor"],
                               "accuracy": i["accuracy"]} for i in ivs],
                "cameras": [{"label": c["camera"], "partial": False, "verified": c["verified"],
                             "sensor": c["sensor"], "accuracy": c["accuracy"]} for c in cams],
                "notes": notes}

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
        needed. A saved drawing of the picture (only ever the store's own: see Setup.store)
        keeps the line's position for the learning examples, but is not drawn over
        RetailNext's own line (see marked())."""
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
            before = {c["picture"]: c["sensor"] for c in self.state["cameras"]}
            for c in cams:  # a picture's camera renamed: its hand counts go with it
                old = before.get(c["picture"])
                if old and old != c["sensor"] and old not in names:
                    self._rename_camera(old, c["sensor"])
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

    # ---- the record of a check ---------------------------------------------------------

    def checked_work(self) -> int:
        """How much a person has answered or added in the current check."""
        return len(self.state["answers"]) + len(self.state["added"])

    def _reviewer(self) -> str:
        return str(self.state["store"].get("operator") or "")

    def _decide(self, action: str, **info: Any) -> None:
        """The audit trail of the current check: every decision, who made it and when."""
        self.state["decisions"].append({"at": _now(), "by": self._reviewer(), "action": action,
                                        **info})

    def _archive(self, reason: str) -> Path | None:
        """Keep the current check, and a copy of its report, before it is replaced."""
        if not self.checked_work() and not self.state.get("report"):
            return None
        folder = self.dir / "history"
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"{datetime.now().astimezone():%Y%m%d-%H%M%S}-{reason}"
        k = 1
        while (folder / f"{stem}.json").exists():
            k += 1
            stem = f"{stem.rsplit('~', 1)[0]}~{k}"
        report = self.state.get("report") or {}
        kept = None
        if report.get("path") and Path(report["path"]).is_file():
            kept = folder / f"{stem}.pptx"
            shutil.copy2(report["path"], kept)
        c = self.counts()
        out = folder / f"{stem}.json"
        write_json_atomic(out, {**self.state, "archived_at": _now(), "archive_reason": reason,
                                "report_copy": str(kept) if kept else None,
                                "verified": c["verified"], "unsure": c["unsure"]})
        return out

    def history(self) -> list[dict[str, Any]]:
        """Earlier checks of this video, newest first."""
        out = []
        for p in sorted((self.dir / "history").glob("*.json"), reverse=True):
            try:
                h = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"file": str(p), "archived_at": h.get("archived_at"),
                        "reason": h.get("archive_reason"), "verified": h.get("verified"),
                        "answers": len(h.get("answers", {})),
                        "by": h.get("store", {}).get("operator", "")})
        return out

    # ---- counting ----------------------------------------------------------------------

    def start(self, confirm: bool = False) -> None:
        """Run the automatic count. Running a checked count again needs confirm: the check
        starts afresh, and the old one is kept in the history folder first."""
        with self._lock:
            if self.manual():
                raise WizardError("This is a manual count: there is nothing to run.")
            if self._job is not None and self._job.running:
                raise WizardError("The count is already running.")
            if not self.state["cameras"]:
                raise WizardError("Draw the cameras first.")
            if not self.dirs():
                raise WizardError("Choose Traffic In or Traffic Out first.")
            if self.checked_work() and not confirm:
                raise WizardError(f"This count has been checked ({self.checked_work()} answers "
                                  f"and added crossings). Running it again starts a new check; "
                                  f"the answers are kept in the video's history but do not "
                                  f"carry over.")
            commands = self._commands(self)
            archived = self._archive("rerun")
            self.state["job"] = {"status": "running", "started_at": _now(), "finished_at": None,
                                 "error": None, "log": []}
            self.state.update(answers={}, people={}, added=[], watched=[], watch=None, report=None,
                              decisions=[])
            self._decide("run", model=self.state["model"], app_version=app_version(),
                         kept=str(archived) if archived else None)
            self._save()
            self._job = _Job(commands, Progress(cameras=len(self.state["cameras"])),
                             self._finished)

    def _finished(self, status: str, error: str | None, log: list[str]) -> None:
        with self._lock:
            self.state["job"].update(status=status, error=error, finished_at=_now(), log=log)
            self.state["job"]["stats"] = self._job_stats()
            self._save()

    def _detector(self) -> dict[str, Any]:
        """The detector settings that proposed the crossings, as detect.py recorded them."""
        for cam in self.state["cameras"]:
            if det := self._read(cam, "candidates").get("detector"):
                return dict(det)
        return {}

    def _job_stats(self) -> dict[str, Any]:
        """Kept with the count, on this computer: what ran, and how fast."""
        job = self.state["job"]
        try:
            took: float | None = (datetime.fromisoformat(job["finished_at"])
                                  - datetime.fromisoformat(job["started_at"])).total_seconds()
        except (TypeError, ValueError):
            took = None
        video_s = float(self.state["duration_s"])
        det = self._detector()
        return {"app_version": app_version(), "model": det.get("model", self.state["model"]),
                "detector": det, "video_s": round(video_s, 1),
                "processing_s": round(took, 1) if took else None,
                "realtime_x": round(video_s / took, 2) if took else None}

    def _made_with(self) -> str:
        """Which detector and build proposed the crossings, and how long that took."""
        stats = self.state["job"].get("stats") or {}
        model = stats.get("model") or self._detector().get("model") or self.state["model"]
        text = f"Crossings proposed by the {model} detector"
        if stats.get("app_version"):
            text += f" (CrossingCount {stats['app_version']})"
        if stats.get("processing_s"):
            text += (f", in {_dur(stats['processing_s'])} for {_dur(stats['video_s'])} of "
                     f"footage ({stats['realtime_x']:g}× real time)")
        if "yolo11s" in str(model):
            text += ". This fast detector merges more people walking together"
        return text + "."

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
        dur = float(self.state["duration_s"])
        items: list[dict[str, Any]] = []
        for cam in self.state["cameras"]:
            items += review_items(cam["sensor"], cam["picture"], self._read(cam, "candidates"),
                                  self._read(cam, "discarded"), self._read(cam, "unexplained"),
                                  self.dirs(), dur)
        items.sort(key=lambda i: (i["kind"] != "counted",
                                  prompt_priority(i["why"]) if i["kind"] == "possible" else 0,
                                  i["t"], i["picture"]))
        mark_twins(items)
        return items

    def watch_ranges(self) -> list[dict[str, Any]]:
        """Movement near the line that produced no count and no likely miss."""
        if self.state["job"]["status"] != "done":
            return []
        out = [r for cam in self.state["cameras"]
               for r in watch_stretches(cam["sensor"], cam["picture"], self._read(cam, "unexplained"))]
        out.sort(key=lambda r: (r["start"], r["picture"]))
        return out

    def answer(self, item_id: str, answer: str | None, people: int = 1) -> None:
        """The checker's answer. A question is about a moment, not one person: "yes" with
        people above 1 means a group crossed together there, each of them counted."""
        if answer not in ("yes", "no", "unsure", None):
            raise WizardError("answer yes, no or unsure")
        if not 1 <= people <= MAX_GROUP:
            raise WizardError(f"a group is 1 to {MAX_GROUP} people")
        item = next((i for i in self.check_items() if i["id"] == item_id), None)
        if item is None:
            raise WizardError(f"there is no crossing {item_id} to check")
        with self._lock:
            self.state["people"].pop(item_id, None)
            if answer is None:
                self.state["answers"].pop(item_id, None)
            else:
                self.state["answers"][item_id] = answer
                if answer == "yes" and people > 1:
                    self.state["people"][item_id] = people
            self._decide("answer" if answer else "undo", id=item_id, answer=answer,
                         people=people if answer == "yes" else None,
                         item={k: item[k] for k in ("kind", "why", "camera", "t", "direction")})
            self._save()

    def add(self, sensor: str, t: float, direction: str, range_id: str | None = None) -> None:
        self._camera(sensor)
        if direction not in self.dirs():
            raise WizardError(f"This count is for {' and '.join(LABELS[d] for d in self.dirs())}.")
        if not 0.0 <= t <= float(self.state["duration_s"]) + 1.0:
            raise WizardError(f"{t:.2f} s is outside the video")
        with self._lock:
            self.state["added"].append({"camera": sensor, "t": round(float(t), 2),
                                        "direction": direction, "range": range_id, "at": _now(),
                                        "by": self._reviewer()})
            self._decide("add", camera=sensor, t=round(float(t), 2), direction=direction,
                         range=range_id)
            self._save()

    def remove_added(self, index: int) -> None:
        with self._lock:
            if not 0 <= index < len(self.state["added"]):
                raise WizardError("no such added crossing")
            removed = self.state["added"].pop(index)
            self._decide("remove_added", entry=removed)
            self._save()

    def set_watch(self, mode: str | None) -> None:
        if mode not in ("doing", "done", "skipped", None):
            raise WizardError("watch mode must be doing, done or skipped")
        with self._lock:
            self.state["watch"] = mode
            self._decide("watch", mode=mode)
            self._save()

    def mark_watched(self, range_id: str, done: bool = True) -> None:
        with self._lock:
            watched = [r for r in self.state["watched"] if r != range_id]
            self.state["watched"] = [*watched, range_id] if done else watched
            self._decide("watched" if done else "not_watched", range=range_id)
            self._save()

    def counts(self) -> dict[str, Any]:
        """The numbers, and whether the check is complete enough to give an accuracy.

        Complete means every crossing the tool counted or listed as a possible miss was
        answered, and every stretch of movement near the line that it could not explain
        was watched (a hand count: at least MIN_WATCHED_PCT of each camera's footage).
        Otherwise someone the tool never detected may be missing from the verified count,
        so the report gives no accuracy. Crossings answered "unsure" are not counted; the
        count and the accuracy are given for both ways they could go.
        """
        dirs = self.dirs()
        c: dict[str, Any]
        if self.manual():
            m = self.state["manual"]
            cams = self.manual_summary()["cameras"]
            verified = {d: sum(1 for x in m["counts"] if x["direction"] == d) for d in dirs}
            c = {"manual": True, "detected": dict.fromkeys(dirs, 0), "verified": verified,
                 "unsure": dict.fromkeys(dirs, 0), "confirmed": 0, "rejected": 0, "found": 0,
                 "not_found": 0, "added": 0, "unanswered": 0, "items": 0, "watch_ranges": 0,
                 "watch_s": 0.0, "watch_done": 0, "groups": 0, "group_people": 0,
                 "watched_pct": {cam["sensor"]: cam["watched_pct"] for cam in cams},
                 "unwatched_s": round(sum(b - a for cam in cams for a, b in cam["unwatched"]), 1),
                 "checked": bool(m["done"])}
            problems = [] if m["done"] else ["Counting is not finished."]
            problems += [f"Only {cam['watched_pct']:.0f}% of {cam['sensor']}'s footage was "
                         f"watched." for cam in cams if cam["watched_pct"] < MIN_WATCHED_PCT]
            return self._completeness(c, problems)
        answers = self.state["answers"]
        c = {"detected": dict.fromkeys(dirs, 0), "verified": dict.fromkeys(dirs, 0),
             "unsure": dict.fromkeys(dirs, 0), "confirmed": 0, "rejected": 0, "found": 0,
             "not_found": 0, "added": 0, "unanswered": 0, "groups": 0, "group_people": 0}
        people = self.state["people"]
        items = self.check_items()
        for it in items:
            if it["kind"] == "counted":
                c["detected"][it["direction"]] += 1
            a = answers.get(it["id"])
            if a is None:
                c["unanswered"] += 1
                continue
            if a == "unsure":
                c["unsure"][it["direction"]] += 1
                continue
            if a == "yes":
                n = int(people.get(it["id"], 1))
                c["verified"][it["direction"]] += n
                if n > 1:
                    c["groups"] += 1
                    c["group_people"] += n
            if it["kind"] == "counted":
                c["confirmed" if a == "yes" else "rejected"] += 1
            else:
                c["found" if a == "yes" else "not_found"] += 1
        for a in self.state["added"]:
            if a["direction"] in dirs:
                c["verified"][a["direction"]] += 1
                c["added"] += 1
        ranges = self.watch_ranges()
        unwatched = [r for r in ranges if r["id"] not in self.state["watched"]]
        c["items"] = len(items)
        c["watch_ranges"] = len(ranges)
        c["watch_s"] = round(sum(r["end"] - r["start"] for r in ranges), 1)
        c["watch_done"] = len(ranges) - len(unwatched)
        c["unwatched_s"] = round(sum(r["end"] - r["start"] for r in unwatched), 1)
        c["checked"] = (self.state["job"]["status"] == "done" and c["unanswered"] == 0
                        and (self.state["watch"] in ("done", "skipped") or not ranges))
        problems = []
        if self.state["job"]["status"] != "done":
            problems.append("The automatic count has not finished.")
        if c["unanswered"]:
            problems.append(f"{c['unanswered']} crossing(s) were not checked.")
        if unwatched:
            problems.append(f"{len(unwatched)} of {len(ranges)} stretches of movement near the "
                            f"line that the tool could not explain were not watched "
                            f"({_dur(c['unwatched_s'])} of footage).")
        return self._completeness(c, problems)

    def _completeness(self, c: dict[str, Any], problems: list[str]) -> dict[str, Any]:
        sensor = self.state["sensor"]
        c["accuracy"] = {d: sensor_accuracy(sensor.get(d), c["verified"][d]) for d in self.dirs()}
        c["accuracy_range"] = {d: accuracy_range(sensor.get(d), c["verified"][d], c["unsure"][d])
                               for d in self.dirs()}
        c["incomplete"] = problems
        c["status"] = "incomplete" if problems else "complete"
        return c

    def unsure_rows(self) -> list[dict[str, Any]]:
        """Crossings the checker could not decide: listed in the report, never counted."""
        if self.manual():
            return []
        answers = self.state["answers"]
        return [{"t": it["t"], "camera": it["camera"], "picture": it["picture"],
                 "direction": it["direction"], "point": it["point"],
                 "found": "Unclear to the checker: not counted"}
                for it in self.check_items() if answers.get(it["id"]) == "unsure"]

    def verified_rows(self) -> list[dict[str, Any]]:
        if self.manual():
            rows = [{"t": c["t"], "camera": c["camera"],
                     "picture": self._camera(c["camera"])["picture"], "direction": c["direction"],
                     "point": None, "found": "Counted by hand"}
                    for c in self.state["manual"]["counts"] if c["direction"] in self.dirs()]
            rows.sort(key=lambda r: (r["t"], r["picture"]))
            return rows
        answers, people = self.state["answers"], self.state["people"]
        rows = [{"t": it["t"], "camera": it["camera"], "picture": it["picture"],
                 "direction": it["direction"], "point": it["point"],
                 "people": int(people.get(it["id"], 1)),
                 "found": _found(it["why"], int(people.get(it["id"], 1)))}
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
                    "run_dir": str(self.run_dir), "history": self.history(),
                    "intervals": self.intervals(), "comparison": self.comparison()}

    # ---- pictures ----------------------------------------------------------------------

    def _frame(self, t: float) -> Image:
        t = max(0.0, min(t, float(self.state["duration_s"]) - 0.2))
        frames = vid.grab_frames_at(self.video, [t])
        if not frames:
            raise WizardError(f"could not read the video at {t:.1f} s")
        return frames[0].image

    def _draw_geometry(self, img: Image, g: dict[str, Any]) -> None:
        if not g["line"] or self.marked():  # RetailNext's own line is in the picture already
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
        numbers = row_numbers(rows)  # the same numbers as the report's table
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
            thumbs.append({"path": str(p), "label": f"{numbers[n - 1]} · {r['direction'].upper()} "
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
                    caption: str, thumbs: list[dict[str, str]],
                    unsure: list[dict[str, Any]] | None = None) -> dict[str, Any]:
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
            (f"{store['operator'] or 'A person'} checked every crossing the tool found: "
             f"{c['confirmed']} confirmed, "
             f"{c['rejected']} rejected. Of {c['found'] + c['not_found']} possible misses it "
             f"listed, {c['found']} were real."),
            watched,
            self._system_line(),
        ]
        method.insert(2, self._made_with())
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
        if c.get("groups"):
            method.insert(3, f"{c['groups']} of the confirmed crossings were groups crossing "
                             f"together: {c['group_people']} people, each counted.")
        unsure = unsure or []
        if unsure:
            method.insert(3, f"{len(unsure)} crossing(s) were unclear to the checker. They are "
                             f"listed below but not in the verified count; the count and the "
                             f"accuracy are given for both ways they could go.")
        if c["incomplete"]:
            method.insert(0, "VALIDATION INCOMPLETE. " + " ".join(c["incomplete"]) + " No accuracy "
                          "is given: people in footage nobody watched may be missing from the count.")
        method.append(f"Report made with CrossingCount {app_version()}, {__copyright__}.")
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
                            "unsure": c["unsure"][d], "system": st["sensor"][d],
                            "accuracy": c["accuracy"][d]["accuracy_pct"],
                            "accuracy_range": c["accuracy_range"][d]} for d in self.dirs()],
            "complete": not c["incomplete"], "incomplete": c["incomplete"],
            "breakdown": self._breakdown(),
            "frame": str(frame), "frame_caption": caption,
            "crossings": [{"n": n, "time": self.clock(r["t"]), "camera": r["camera"],
                           "direction": LABELS[r["direction"]], "found": r["found"]}
                          for n, r in [*zip(row_numbers(rows), rows), *(("?", u) for u in unsure)]],
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
            data = self.report_data(c, rows, frame, caption, thumbs, self.unsure_rows())
            start, end = self._period()
            when = f"{start:%Y-%m-%d %H%M}-{end:%H%M}" if start and end else "report"
            words = f"{store['code']} {store['name']} camera validation {when}"
            out = self.dir / (re.sub(r"[^A-Za-z0-9 ._-]+", "-", words).strip() + ".pptx")
            build_report(data, out, logo)
            self.state["report"] = {"path": str(out), "made_at": _now()}
            self._save()
            return out
