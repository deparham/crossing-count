"""Guided count: pick the footage, draw, count automatically, check, report.

The tool finds the crossings; a person then confirms each one it found and each
likely miss it lists (and can also watch the movement it could not explain), so
the report's number is a verified count. Detection runs gate.py and detect.py as
child processes, and their progress is read from what they print.
"""

from __future__ import annotations

import functools
import json
import pickle
import re
import shutil
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Concatenate, ParamSpec, TypeVar

import cv2
import numpy as np
from numpy.typing import NDArray

from .. import (
    __copyright__,
    auditlog,
    evaluate,
    independence,
    paths,
    provenance,
    runs,
    sampling,
    sensors,
    validation,
)
from .. import correspondence as co
from .. import video as vid
from ..config import ConfigError, bind, load_config
from ..export import INTERVAL_MIN, sensor_accuracy
from ..gating import camera_dir
from ..manual import intervals_for, merge_ranges, unwatched_ranges
from ..report_pdf import build_pdf
from ..report_pptx import build_report
from ..util import default_run_dir, fmt_hms, same_store, write_json_atomic
from ..version import app_version
from . import schema
from .items import (
    CAUSES,
    CHOICES,
    DETECTOR_CHOICES,
    DIRECTIONS,
    FOUND,
    GROUND_TRUTH_SPEC,
    LABELS,
    MAX_GROUP,
    MIN_WATCHED_PCT,
    MODELS,
    PEOPLE,
    RULE_CHOICES,
    SHORTFALL_S,
    SMALL_MODELS,
    TOTAL_ACTIONS_KEPT,
    TWIN_WINDOW_S,
    _found,
    accuracy_range,
    distinct_people,
    mark_twins,
    prompt_priority,
    review_items,
    row_numbers,
    unavailable,
    watch_stretches,
)
from .jobs import Progress, _Job, pipeline_commands

Image = NDArray[np.uint8]


class WizardError(Exception):
    """A step that cannot be done yet, or input that does not make sense."""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _pts(a: Any) -> list[list[float]]:
    return [[round(float(x), 1), round(float(y), 1)] for x, y in a]


def _dur(seconds: float) -> str:
    s = max(0, round(seconds))
    return f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"


# ---- the count ---------------------------------------------------------------------------

P = ParamSpec("P")
R = TypeVar("R")


def _open_only(fn: Callable[Concatenate[Wizard, P], R]) -> Callable[Concatenate[Wizard, P], R]:
    """A change a finalised validation refuses: it is corrected in a new version (runs.py)."""
    @functools.wraps(fn)
    def wrapper(self: Wizard, /, *args: P.args, **kwargs: P.kwargs) -> R:
        self._check_open()
        return fn(self, *args, **kwargs)
    return wrapper


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
        self.root = root  # the data folder: runs, the audit log, finalised validations
        self._commands = commands or pipeline_commands
        self._lock = threading.RLock()
        self._job: _Job | None = None
        info = vid.probe(self.video)
        self.fps = float(info.fps_reported or 0) or 10.0  # for stepping one frame at a time
        self.filled_in: list[str] = []  # what an older state file did not have (schema.py)
        self.state: dict[str, Any]
        if self.path.is_file():
            try:
                self.state, self.filled_in = schema.load_state(self.path, info.fingerprint)
            except schema.StateError as exc:
                raise WizardError(str(exc)) from None
            if self.state["job"]["status"] == "running":  # the app was closed during a count
                self.state["job"].update(status="stopped", error="The count was interrupted.")
        else:
            audit = vid.audit_timebase(self.video)
            interval = vid.parse_filename_interval(info.filename)
            self.state = {
                "schema": schema.SCHEMA, "video": str(self.video), "filename": info.filename,
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
            schema.migrate(self.state)
            self._save()

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

    @_open_only
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
            before = [c["sensor"] for c in self.state["cameras"]]
            self.state["cameras"] = [
                {"picture": i, "sensor": c["sensor"], "site": c["site"], "config": c["path"],
                 "tile": tiles[i], "matched_by": c.get("how"),
                 "confident": bool(c.get("confident", True))} for i, c in sorted(chosen.items())]
            self._decide("cameras", before=before, after={
                c["sensor"]: c["config"] for c in self.state["cameras"]})
            store = self.state["store"]
            if not store["code"]:
                store["code"] = next(iter(chosen.values()))["site"]
            if not store["name"]:
                store["name"] = self._stores().get(store["code"], "")
            self._save()

    @_open_only
    def set_direction(self, direction: str) -> None:
        if direction not in CHOICES:
            raise WizardError("Choose Traffic In, Traffic Out or both.")
        with self._lock:
            if self.state["direction"] != direction:
                self._decide("traffic", before=self.state["direction"], after=direction)
            self.state["direction"] = direction
            self._save()

    @_open_only
    def set_rules(self, children: str, staff: str) -> None:
        """What counts as a person (Ground Truth Specification, section 4), set to match
        what the sensor is set up to count."""
        with self._lock:
            for name, value in (("children", children), ("staff", staff)):
                if value not in RULE_CHOICES:
                    raise WizardError(f"{name}: choose count or exclude")
            self.state["rules"] = {"children": children, "staff": staff}
            self._decide("rules", children=children, staff=staff)
            self._save()

    @_open_only
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
                    raise WizardError(f"Enter the system's number for {missing[0]}.")
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
            self._decide("system_numbers_set", after=self._sensor_numbers())
            self._save()

    def _sensor_numbers(self) -> dict[str, Any]:
        return {"sensor": dict(self.state["sensor"]),
                "intervals": dict(self.state["sensor_intervals"]),
                "cameras": dict(self.state["sensor_cameras"])}

    def use_retailnext(self, got: dict[str, Any]) -> list[str]:
        """RetailNext's numbers from its API (retailnext.camera_counts), through its adapter
        (on the footage's own day when the answer does not say which)."""
        start, _ = self._period()
        if start is None:
            raise WizardError("The video's name has no clock time, so RetailNext's numbers "
                              "cannot be lined up with it.")
        try:
            data = sensors.from_retailnext(got, start.date())
        except sensors.SensorError as exc:
            raise WizardError(str(exc)) from exc
        return self.use_sensor(data)

    @_open_only
    def use_sensor(self, data: sensors.SensorData) -> list[str]:
        """A counting system's numbers in the normalised form (sensors.py), whatever the
        system: the cameras' sum per 15-minute interval, and each camera's own. Every
        interval must be covered exactly once. Returns warnings for numbers the source
        marked as not full counts."""
        ivs = self.intervals()
        keys = [i["key"] for i in ivs]
        if keys == ["all"]:
            raise WizardError(f"The video's name has no clock time, so {data.system}'s numbers "
                              f"cannot be lined up with it.")
        dirs = self.dirs()
        names = {c["sensor"].lower(): c["sensor"] for c in self.state["cameras"]}
        by_cam: dict[str, list[sensors.IntervalCount]] = {}
        for c in data.counts:
            key = names.get(c.camera.lower()) if c.camera else ""
            if key is not None and c.direction in dirs:  # not another camera of the store
                by_cam.setdefault(key, []).append(c)
        own = {k: v for k, v in by_cam.items() if k}
        if own and len(own) < len(names) and "" not in by_cam:
            gone = [n for n in names.values() if n not in own]
            raise WizardError(f"{data.system} has no numbers for {', '.join(gone)}.")
        sources = own if own and len(own) == len(names) else {"": by_cam.get("", [])}
        per_iv = {k: dict.fromkeys(dirs, 0) for k in keys}
        per_cam: dict[str, dict[str, int]] = {}
        warnings = list(data.notes)
        step = timedelta(minutes=INTERVAL_MIN)
        for cam, counts in sources.items():
            label = cam or str(data.detail.get("store") or "the cameras together")
            if cam:
                per_cam[cam] = dict.fromkeys(dirs, 0)
            for i in ivs:  # wall-clock times: the footage's clock may carry a time zone
                s = datetime.fromisoformat(i["start"]).replace(tzinfo=None)
                inside = [c for c in counts if c.start >= s and c.end <= s + step]
                for d in dirs:
                    mine = [c for c in inside if c.direction == d]
                    covered = sum((c.end - c.start).total_seconds() for c in mine)
                    if covered < step.total_seconds() - 1:
                        raise WizardError(f"{data.system} gave no number for {label} at "
                                          f"{i['key']}.")
                    if covered > step.total_seconds() + 1:
                        raise WizardError(f"{data.system} gave {label} at {i['key']} more than "
                                          f"one number for {LABELS[d]}.")
                    n = sum(c.count for c in mine)
                    per_iv[i["key"]][d] += n
                    if cam:
                        per_cam[cam][d] += n
                for c in inside:
                    if c.validity != "complete":
                        warnings.append(f"{data.system} marked {label} {c.start:%H:%M}–"
                                        f"{c.end:%H:%M} as {c.validity}: its number there is "
                                        f"not a full count.")
        warnings = list(dict.fromkeys(warnings))  # once per interval, not per direction
        if len(keys) > 1:
            self.set_sensor(intervals={k: dict(v) for k, v in per_iv.items()},
                            cameras={c: dict(v) for c, v in per_cam.items()})
        else:
            self.set_sensor(values=dict(per_iv[keys[0]]),
                            cameras={c: dict(v) for c, v in per_cam.items()})
        with self._lock:
            self.state["sensor_source"] = {
                "source": data.source, "system": data.system, **data.detail,
                "cameras": list(per_cam) or list(data.detail.get("cameras") or [])
                or ["all cameras together"],
                "fetched_at": _now(), "warnings": warnings, "numbers": self._sensor_numbers()}
            self._decide("sensor_numbers", source=data.source, system=data.system)
            self._save()
        return warnings

    @_open_only
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
            self.state["retailnext"] = {**(self.state.get("retailnext") or {}),
                                        **{k: v for k, v in info.items() if k != "sampling"}}
            if info.get("sampling"):  # which window of the day this is, and why (sampling.py)
                self.state["sampling"] = info["sampling"]
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
        if self.state.get("final"):  # a finalised validation is never changed
            return notes
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

    def note_marks_detected(self, evidence: list[dict[str, Any]]) -> None:
        """What each picture shows (Setup.marks()): RetailNext's lines, or none. Kept so a
        count's independence rests on the picture, not only on an answer."""
        if self.state.get("final"):
            return
        with self._lock:
            brief = [{k: e.get(k) for k in ("marks", "segments", "length_px")} for e in evidence]
            if self.state.get("marks_detected") != brief:
                self.state["marks_detected"] = brief
                self._save()

    def _download(self) -> dict[str, Any]:
        """What this footage is, as the download remembered it: from the state when the app
        opened it, else from the downloads file beside it (retailnext.download_info)."""
        from .. import retailnext as rn

        known = dict(self.state.get("retailnext") or {})
        if known.get("twin") or known.get("start"):
            return known
        return {**(rn.download_info(self.video) or {}), **known}

    def twin(self) -> Path | None:
        """The same window downloaded the other way (clean beside marked), when there is one:
        the tool counts the clean footage while a person counts the marked one (or the other
        way about), and the marked one shows where RetailNext's line is."""
        name = str(self._download().get("twin") or "")
        if not name:
            return None
        beside = self.video.parent / name
        return beside if beside.is_file() else None

    def count_video(self) -> Path:
        """The footage the tool counts. Hand-counting footage with RetailNext's marks on it,
        the tool counts the clean twin instead: its pictures are what it meets in use, and
        nothing of the sensor's is in them."""
        twin = self.twin()
        if twin is not None and self.manual() and self.footage_marked():
            return twin
        return self.video

    def sealed(self) -> bool:
        """While a person is counting by hand, what the tool found is not shown: seeing it
        would turn counting into checking the tool's work."""
        return self.manual() and not self.state["manual"].get("done")

    def footage(self) -> dict[str, Any]:
        """Is the footage clean of RetailNext's marks, and how do we know (independence.py)?"""
        return independence.of_state(self.state)

    def footage_marked(self) -> bool:
        """Counted or checked on footage showing the sensor's own work, whatever the mode: the
        person could see the sensor's answer, so the count is not independent of it."""
        return not self.footage()["clean"]

    def correspondence(self) -> dict[str, dict[str, Any]]:
        """How each camera's counting line corresponds to the sensor's (correspondence.py),
        and how the drawing was matched to the picture."""
        out: dict[str, dict[str, Any]] = {}
        for cam in self.state["cameras"]:
            found: dict[str, Any]
            if self.marked():  # RetailNext's own line is in the picture: its line by construction
                found = {"method": "sensor_line", "line_match": None,
                         "words": co.METHODS["sensor_line"]}
            else:
                cfg = None
                if cam.get("config"):
                    try:
                        cfg = load_config(Path(cam["config"]))
                    except (ConfigError, OSError, ValueError, KeyError):
                        cfg = None
                found = (co.of_config(cfg.traced_on, cfg.overlay_hsv) if cfg is not None
                         else {"method": "by_eye", "line_match": None,
                               "words": "no drawing: the camera was named only"})
            out[cam["sensor"]] = {**found, "strength": co.STRENGTH[found["method"]],
                                  "matched_by": cam.get("matched_by"),
                                  "confirmed_by": cam.get("confirmed_by")}
        return out

    def unconfirmed(self) -> list[str]:
        """Cameras matched to a picture in a way the tool is not sure of, and which nobody has
        confirmed. Counting them would put one camera's crossings against another camera's
        numbers, which is a wrong report rather than a failed run."""
        return [f"Picture {c['picture'] + 1} was matched to {c['sensor']} by "
                f"{c.get('matched_by') or 'a guess'}. Check that it is that camera and confirm "
                f"it on the Cameras step." for c in self.state["cameras"]
                if c.get("confident") is False and not c.get("confirmed_by")]

    @_open_only
    def confirm_camera(self, picture: int) -> None:
        """A person looked and says this picture is that camera."""
        with self._lock:
            cam = next((c for c in self.state["cameras"] if int(c["picture"]) == int(picture)), None)
            if cam is None:
                raise WizardError(f"There is no camera in picture {int(picture) + 1}.")
            cam["confirmed_by"] = self._reviewer() or "someone"
            cam["confirmed_at"] = _now()
            self._decide("camera_confirmed", picture=int(picture), camera=cam["sensor"],
                         matched_by=cam.get("matched_by"))
            self._save()

    def _check_open(self) -> None:
        final = self.state.get("final")
        if final:
            raise WizardError(f"This validation is finalised as {final['id']} and can no longer "
                              f"change. To correct it, start a new version: the finalised one "
                              f"stays as it is.")

    def finalise(self, shared: Path | None = None) -> dict[str, Any]:
        """Give this validation an ID and keep it, with everything needed to reproduce it,
        in a folder that is never changed again (runs.py). Its report must be made first."""
        with self._lock:
            self._check_open()
            try:
                run = runs.write(self, self.root, shared)
            except runs.RunError as exc:
                raise WizardError(str(exc)) from exc
            self.state["final"] = {"id": run["validation_id"], "at": run["finalised_at"],
                                   "folder": run["folder"],
                                   "manifest_sha256": run["manifest_sha256"]}
            self._decide("finalised", after={"validation_id": run["validation_id"],
                                             "manifest_sha256": run["manifest_sha256"]})
            self._save()
            return run

    def new_version(self) -> None:
        """Open a finalised validation again, as a new version, to correct it. The finalised
        one stays exactly as it was; the new one names it, and gets its own ID when it is
        finalised."""
        with self._lock:
            final = self.state.get("final")
            if not final:
                raise WizardError("This validation is not finalised.")
            self._archive(f"finalised {final['id']}")
            self.state.pop("final")
            self.state.pop("result", None)
            self.state.update(supersedes=final["id"], report=None)
            self._decide("new_version", before=final["id"])
            self._save()

    def period(self) -> tuple[datetime | None, datetime | None]:
        """The footage's clock period, from its name."""
        return self._period()

    def _system_line(self) -> str:
        src = self.state.get("sensor_source")
        system = (src or {}).get("system") or "RetailNext"
        sub = f", subscription {src['subscription']}" if src and src.get("subscription") else ""
        if src and src.get("source") == "RetailNext API":
            how = (f"fetched from RetailNext's API ({src['store']}{sub}: "
                   f"{' + '.join(src['cameras'])}) on {str(src['fetched_at'])[:10]}")
        elif src:
            how = (f"imported from {src.get('file') or 'a file'} ({' + '.join(src['cameras'])}) "
                   f"on {str(src['fetched_at'])[:10]}")
        else:
            how = f"typed in from {system}"
        text = (f"System count: {system}, same cameras and period, {how}. Sensor accuracy = "
                f"100% minus the system's error as a share of the verified count.")
        return text + "".join(f" {w}" for w in (src or {}).get("warnings", []))

    def intervals(self) -> list[dict[str, Any]]:
        """RetailNext's 15-minute intervals this footage overlaps, and how much of each."""
        cs = self.state["clock_start"]
        return intervals_for(datetime.fromisoformat(cs) if cs else None,
                             float(self.state["duration_s"]))

    def coverage(self) -> dict[str, Any] | None:
        """How much of the period the system's numbers cover this footage actually covers.
        RetailNext counts whole 15-minute intervals; an export short of its window compares
        its full number against less footage than it describes. Short by up to SHORTFALL_S the
        difference is beneath the counts' own noise. None when there is no clock."""
        ivs = self.intervals()
        if not self.state.get("clock_start") or not ivs:
            return None
        whole = len(ivs) * INTERVAL_MIN * 60
        got = sum(float(i["covered_s"]) for i in ivs)
        return {"footage_s": round(got, 1), "period_s": whole, "missing_s": round(whole - got, 1),
                "pct": round(100.0 * got / whole, 1) if whole else None,
                "full": whole - got <= SHORTFALL_S,
                "intervals": [i["label"] for i in ivs]}

    def coverage_words(self) -> str | None:
        """The shortfall in words, for the report's first page and the checks."""
        c = self.coverage()
        if c is None or c["full"]:
            return None
        system = (self.state.get("sensor_source") or {}).get("system") or "RetailNext"
        return (f"{system} counts whole 15-minute intervals: its number covers "
                f"{c['period_s'] / 60:g} minutes, the footage {c['footage_s'] / 60:.1f} of them "
                f"({c['pct']}%). The {c['missing_s'] / 60:.1f} minutes not in the footage are in "
                f"its count but in no one's, so the difference is not like for like.")

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
        if self.total_only():
            # a total says how many crossed, never when, so it belongs to no one interval:
            # the whole footage is the only period it can be set against
            per = self.state["totals"]["by_camera"]
            by_cam = {cam: {d: int(per.get(cam, {}).get(d) or 0) for d in dirs} for cam in by_cam}
            ivs, by_iv = [], {}
        whole = (self.total_by_direction() if self.total_only()
                 else {d: sum(v[d] for v in by_iv.values()) for d in dirs})

        def against(sensor: dict[str, int] | None, verified: dict[str, int]
                    ) -> dict[str, Any] | None:
            return {d: sensor_accuracy(sensor.get(d), verified[d]) for d in dirs} if sensor else None

        s_iv, s_cam = self.state["sensor_intervals"], self.state["sensor_cameras"]
        intervals = [{**i, "verified": by_iv[i["key"]], "sensor": s_iv.get(i["key"]),
                      "accuracy": against(s_iv.get(i["key"]), by_iv[i["key"]])} for i in ivs]
        cameras = [{"camera": c, "verified": v, "sensor": s_cam.get(c),
                    "accuracy": against(s_cam.get(c), v)} for c, v in by_cam.items()]
        overlap = 0 if self.manual() or self.total_only() else sum(
            1 for it in self.check_items() if it.get("twin") and it["twin"]["camera"] != it["camera"])
        diffs = [(abs(a[d]["error"]), i["label"], d, a[d]) for i in intervals
                 if (a := i["accuracy"]) for d in dirs if a[d]["error"] is not None]
        largest = None
        if len(intervals) > 1 and diffs:
            size, label, d, acc = max(diffs, key=lambda x: x[0])
            if size > 0:
                largest = {"interval": label, "direction": d, **acc}
        out = {"intervals": intervals, "cameras": cameras, "overlap": overlap, "largest": largest,
               "verified": whole, "total_only": self.total_only()}
        rows = self._rows(intervals, whole)
        return {**out, "rows": rows, "metrics": validation.by_direction(rows) if rows else None}

    def _rows(self, intervals: list[dict[str, Any]],
              whole: dict[str, int]) -> list[dict[str, Any]]:
        """Verified against the system as the validation engine reads it: each whole 15-minute
        interval with the system's number (one only partly in the footage does not compare
        like for like), or the whole footage when there is one number for it.

        A total-only count is always the second of these, and it is one row however many
        intervals the footage spans: one number counted, one number to set it against."""
        dirs, cams = self.dirs(), len(self.state["cameras"])
        if self.state["sensor_intervals"] and len(intervals) > 1:
            return [{"interval": i["label"], "start": i["start"], "covered_s": i["covered_s"],
                     "cameras": cams, "direction": d, "truth": int(i["verified"][d]),
                     "system": int(i["sensor"][d])}
                    for i in intervals if i["sensor"] and i["full"] is not False
                    for d in dirs if i["sensor"].get(d) is not None]
        sensor = self.state["sensor"]
        return [{"interval": "whole footage", "start": self.state["clock_start"],
                 "covered_s": float(self.state["duration_s"]), "cameras": cams, "direction": d,
                 "truth": int(whole[d]), "system": int(sensor[d]),
                 **({"reference": "total only"} if self.total_only() else {})}
                for d in dirs if sensor.get(d) is not None]

    def result(self) -> dict[str, Any]:
        """This validation's outcome as data, saved with its report: what was compared with
        what, under which rules, and how complete it was. validation.py puts many together."""
        with self._lock:
            c, comp, st = self.counts(), self.comparison(), self.state
            src = st.get("sensor_source") or {}
            return {
                "schema": "result/1", "engine": validation.ENGINE, "made_at": _now(),
                "app_version": app_version(), "status": c["status"],
                "incomplete": c["incomplete"], "checks": c["checks"],
                "mode": st.get("mode") or "auto", "marked": self.footage_marked(),
                "total_only": self.total_only(),
                "footage": self.footage(), "lines": self.correspondence(),
                "rules": st.get("rules"),
                # a total-only count marks no crossings, so no version of what a crossing is
                # was applied to it
                "specification": None if self.total_only() else (
                    st["manual"].get("specification") if self.manual() else GROUND_TRUTH_SPEC),
                "system": src.get("system") or "RetailNext",
                "source": src.get("source") or "typed in", "subscription": src.get("subscription"),
                "store": {k: st["store"].get(k) for k in ("code", "name", "location")},
                "cameras": [cam["sensor"] for cam in st["cameras"]],
                "fingerprint": st["fingerprint"], "filename": st["filename"],
                "clock_start": st["clock_start"], "duration_s": st["duration_s"],
                "dirs": self.dirs(), "unsure": c["unsure"], "rows": comp["rows"],
                "metrics": comp["metrics"], "operator": st["store"].get("operator"),
                "sampling": sampling.brief(st.get("sampling")), "traffic": self.traffic(c),
                "by_level": validation.by_level(comp["rows"]),
            }

    def traffic(self, c: dict[str, Any]) -> dict[str, Any] | None:
        """How busy the footage was by the verified count, in crossings per camera-hour (the
        directions validated), and its traffic level (validation.traffic_level)."""
        hours = float(self.state["duration_s"]) / 3600 * max(1, len(self.state["cameras"]))
        if not hours:
            return None
        rate = sum(int(c["verified"][d]) for d in self.dirs()) / hours
        return {"per_camera_hour": round(rate, 1), "level": validation.traffic_level(rate),
                "directions": self.dirs()}

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
        m = (comp["metrics"] or {}).get("total") or next(iter((comp["metrics"] or {}).values()), None)
        few = validation.MIN_VERIFIED_FOR_PCT

        def gate(acc: dict[str, Any] | None) -> dict[str, Any] | None:
            """A row's difference as a percentage only on enough verified crossings."""
            return {d: {**a, "error_pct": a["error_pct"] if a["verified"] >= few else None}
                    for d, a in acc.items()} if acc else acc

        if any(a["verified"] < few and a["error_pct"] is not None
               for it in [*ivs, *cams] if it["accuracy"] for a in it["accuracy"].values()):
            notes.append(f"Differences are given as a percentage only where at least {few} "
                         f"crossings were verified; on fewer, one crossing moves it by several "
                         f"points, so the difference is given in people.")
        if ivs and m and m["intervals"] > 1 and m["wape_pct"] is not None and m["truth"] >= few:
            notes.append(f"Across the {m['intervals']} whole intervals, the system's typical error "
                         f"was {m['mae']} people per interval (MAE); its over- and undercounts "
                         f"together came to {m['wape_pct']}% of the verified count (WAPE).")
        return {"dirs": [{"key": d, "label": LABELS[d]} for d in self.dirs()],
                "intervals": [{"label": i["label"], "partial": i["full"] is False,
                               "verified": i["verified"], "sensor": i["sensor"],
                               "accuracy": gate(i["accuracy"])} for i in ivs],
                "cameras": [{"label": c["camera"], "partial": False, "verified": c["verified"],
                             "sensor": c["sensor"], "accuracy": gate(c["accuracy"])} for c in cams],
                "notes": notes}

    @_open_only
    def set_model(self, model: str) -> None:
        if model not in MODELS:
            raise WizardError(f"unknown detector {model!r}")
        if why := unavailable(model):
            raise WizardError(why)
        with self._lock:
            self.state["model"] = model
            self._save()

    @_open_only
    def set_store(self, name: str | None = None, code: str | None = None,
                  location: str | None = None, report_date: str | None = None,
                  operator: str | None = None) -> None:
        with self._lock:
            store = self.state["store"]
            was = dict(store)
            for key, value in (("name", name), ("code", code), ("location", location),
                               ("report_date", report_date), ("operator", operator)):
                if value is not None:
                    store[key] = value.strip()
            changed = [k for k in store if store.get(k) != was.get(k)]
            if changed:
                self._decide("store_details", before={k: was.get(k) for k in changed},
                             after={k: store.get(k) for k in changed})
            if store["name"] and store["code"]:  # remembered for the next video of this store
                stores = self._stores()
                stores[store["code"]] = store["name"]
                write_json_atomic(self.stores_path, stores)
            self._save()

    # ---- manual counting ---------------------------------------------------------------

    def manual(self) -> bool:
        return self.state.get("mode") == "manual"

    def total_only(self) -> bool:
        """A count of the total only: how many crossed, without saying when each one did.

        It is a count, not a count of crossings. Nothing about it can be matched against the
        tool's crossings, so it can never be a gold clip nor give a recall or a precision;
        it compares with the system's number as one total against another.
        """
        return self.state.get("mode") == "total"

    @_open_only
    def set_mode(self, mode: str) -> None:
        if mode not in ("auto", "manual", "total"):
            raise WizardError("Choose automatic counting, counting by hand, or the total only.")
        with self._lock:
            if self.state.get("mode") != mode:
                self._decide("mode", before=self.state.get("mode"), after=mode)
            self.state["mode"] = mode
            self._save()

    @_open_only
    def set_marks(self, marks: bool) -> None:
        with self._lock:
            if self.state.get("marks") != bool(marks):
                self._decide("marked_footage", before=self.state.get("marks"), after=bool(marks))
            self.state["marks"] = bool(marks)
            self._save()

    @_open_only
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
            self._decide("cameras", before=list(before.values()), after=names)
            self.state["cameras"] = cams
            store = self.state["store"]
            if not store["code"]:
                first = names[0]
                store["code"] = next((c["site"] for c in cams if c["site"]), "") or (
                    first.rsplit("-", 1)[0] if first.count("-") >= 2 else first)
            if not store["name"]:
                store["name"] = self._stores().get(store["code"], "")
            self._save()

    @_open_only
    def manual_add(self, sensor: str, t: float, direction: str, uncertain: bool = False,
                   note: str = "") -> dict[str, Any]:
        """One crossing counted by hand. An uncertain one is kept and listed but never counted
        (Ground Truth Specification: when in doubt, mark it, never guess)."""
        self._camera(sensor)
        if direction not in self.dirs():
            raise WizardError(f"This count is for {' and '.join(LABELS[d] for d in self.dirs())}.")
        if not 0.0 <= t <= float(self.state["duration_s"]) + 1.0:
            raise WizardError(f"{t:.2f} s is outside the video")
        with self._lock:
            m = self.state["manual"]
            c = {"id": m["next_id"], "camera": sensor, "t": round(float(t), 2),
                 "direction": direction, "at": _now(),
                 **({"uncertain": True} if uncertain else {}),
                 **({"note": note.strip()[:500]} if note.strip() else {})}
            m["next_id"] += 1
            m["counts"].append(c)
            m["done"] = False
            self._decide("hand_count_added", after=dict(c))
            self._save()
            return c

    @_open_only
    def manual_delete(self, count_id: int) -> None:
        with self._lock:
            m = self.state["manual"]
            kept = [c for c in m["counts"] if c["id"] != count_id]
            if len(kept) == len(m["counts"]):
                raise WizardError("There is no such count.")
            gone = next(c for c in m["counts"] if c["id"] == count_id)
            m["counts"], m["done"] = kept, False
            self._decide("hand_count_removed", before=dict(gone))
            self._save()

    @_open_only
    def manual_edit(self, count_id: int, t: float | None = None, direction: str | None = None,
                    uncertain: bool | None = None, note: str | None = None,
                    reason: str = "") -> dict[str, Any]:
        """Correct one count by hand: its moment, its direction, whether it is certain, its
        note. What it was, what it became and why go to the logs."""
        with self._lock:
            c = next((x for x in self.state["manual"]["counts"] if x["id"] == count_id), None)
            if c is None:
                raise WizardError("There is no such count.")
            before = dict(c)
            if t is not None:
                if not 0.0 <= t <= float(self.state["duration_s"]) + 1.0:
                    raise WizardError(f"{t:.2f} s is outside the video")
                c["t"] = round(float(t), 2)
            if direction is not None:
                if direction not in self.dirs():
                    raise WizardError(f"This count is for "
                                      f"{' and '.join(LABELS[d] for d in self.dirs())}.")
                c["direction"] = direction
            if uncertain is not None:
                if uncertain:
                    c["uncertain"] = True
                else:
                    c.pop("uncertain", None)
            if note is not None:
                if note.strip():
                    c["note"] = note.strip()[:500]
                else:
                    c.pop("note", None)
            if c != before:
                c["edited_at"] = _now()
                self._decide("hand_count_edited", before=before, after=dict(c),
                             reason=reason.strip() or None)
                self._save()
            return dict(c)

    @_open_only
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
        if self.state.get("final"):
            return  # the player still reports what was watched: nothing to keep once finalised
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

    @_open_only
    def manual_done(self, done: bool = True) -> None:
        with self._lock:
            self.state["manual"]["done"] = bool(done)
            if done:  # the definition this count was finished under
                self.state["manual"]["specification"] = GROUND_TRUTH_SPEC
            self._decide("counting_finished" if done else "counting_reopened",
                         after={"counts": len(self.state["manual"]["counts"])})
            self._save()

    # ---- counting the total only -------------------------------------------------------

    def _total_camera(self, sensor: str, direction: str) -> dict[str, int]:
        self._camera(sensor)
        if direction not in self.dirs():
            raise WizardError(f"This count is for {' and '.join(LABELS[d] for d in self.dirs())}.")
        per: dict[str, int] = self.state["totals"]["by_camera"].setdefault(sensor, {})
        return per

    @_open_only
    def total_step(self, sensor: str, direction: str, step: int = 1) -> int:
        """One press of + or -. Never below zero, and what was pressed is kept, so a count
        that looks wrong afterwards can be read back. The moment of the press is not kept:
        a total is not a list of crossings, and a time per press would look like one."""
        if step not in (1, -1):
            raise WizardError("A press counts one person up or down.")
        with self._lock:
            per = self._total_camera(sensor, direction)
            was = int(per.get(direction, 0))
            per[direction] = max(0, was + step)
            t = self.state["totals"]
            if per[direction] != was:
                t["actions"].append({"camera": sensor, "direction": direction, "step": step,
                                     "at": _now()})
                del t["actions"][:-TOTAL_ACTIONS_KEPT]
            t["done"] = False
            self._save()
            return int(per[direction])

    @_open_only
    def total_set(self, sensor: str, direction: str, value: int | None) -> None:
        """A number typed in rather than pressed up, or taken back out."""
        if value is not None and value < 0:
            raise WizardError("A count cannot be negative.")
        with self._lock:
            per = self._total_camera(sensor, direction)
            if value is None:
                per.pop(direction, None)
            else:
                per[direction] = int(value)
            t = self.state["totals"]
            t["actions"].append({"camera": sensor, "direction": direction, "typed": value,
                                 "at": _now()})
            del t["actions"][:-TOTAL_ACTIONS_KEPT]
            t["done"] = False
            self._decide("total_typed", camera=sensor, direction=direction, after=value)
            self._save()

    @_open_only
    def total_notes(self, notes: str) -> None:
        with self._lock:
            self.state["totals"]["notes"] = str(notes).strip()[:2000]
            self._save()

    @_open_only
    def total_done(self, done: bool = True, whole_clip: bool | None = None) -> None:
        """Finished. A total can only be set against the system's number when it covers the
        same footage the system's number describes, so that has to be said, not assumed."""
        with self._lock:
            t = self.state["totals"]
            if whole_clip is not None:
                t["whole_clip"] = bool(whole_clip)
            if done:
                if missing := self.total_missing():
                    raise WizardError(missing[0])
                if not t["whole_clip"]:
                    raise WizardError("Say whether this total is for the whole clip: a total "
                                      "of part of it cannot be set against the system's "
                                      "number for the whole period.")
            t["done"] = bool(done)
            self._decide("total_finished" if done else "total_reopened",
                         after=self.total_by_direction())
            self._save()

    def total_missing(self) -> list[str]:
        """What is still needed before a total-only count is finished."""
        per = self.state["totals"]["by_camera"]
        return [f"Enter {LABELS[d]} for {cam['sensor']}."
                for cam in self.state["cameras"] for d in self.dirs()
                if per.get(cam["sensor"], {}).get(d) is None]

    def total_by_direction(self) -> dict[str, int]:
        """The whole footage's total per direction: every camera's number added up."""
        per = self.state["totals"]["by_camera"]
        return {d: sum(int(per.get(cam["sensor"], {}).get(d) or 0)
                       for cam in self.state["cameras"]) for d in self.dirs()}

    def total_summary(self) -> dict[str, Any]:
        """The total-only count as the page and the report read it."""
        t = self.state["totals"]
        per = t["by_camera"]
        return {"cameras": [{"sensor": cam["sensor"],
                             "counts": {d: per.get(cam["sensor"], {}).get(d) for d in self.dirs()}}
                            for cam in self.state["cameras"]],
                "by_direction": self.total_by_direction(), "notes": t["notes"],
                "done": bool(t["done"]), "whole_clip": bool(t["whole_clip"]),
                "missing": self.total_missing(), "presses": len(t["actions"])}

    @_open_only
    def recount(self) -> None:
        """Start a second, independent count by hand of the same footage, by someone else:
        the first is kept in the history folder (and in the gold set, if it was kept there),
        and nothing of it is shown to the second person."""
        with self._lock:
            if not self.manual():
                raise WizardError("Only a count by hand can be counted again.")
            self._archive("second count")
            self._decide("recount")
            m = self.state["manual"]
            m.update(counts=[], watched={}, positions={}, done=False)
            self.state["store"]["operator"] = ""  # the second person types their own name
            self.state["report"] = None
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
                         "in": sum(1 for c in mine if c["direction"] == "in"
                                   and not c.get("uncertain")),
                         "out": sum(1 for c in mine if c["direction"] == "out"
                                    and not c.get("uncertain")),
                         "uncertain": sum(1 for c in mine if c.get("uncertain")),
                         "watched": watched,
                         "watched_pct": round(min(100.0, 100.0 * seen / dur), 1) if dur else 0.0,
                         "unwatched": unwatched_ranges(watched, dur)})
        return {"cameras": cams, "counts": m["counts"], "positions": m["positions"],
                "done": m["done"]}

    # ---- learning examples -------------------------------------------------------------

    def save_examples(self, root: Path, wait: bool = False) -> None:
        """Save every counted crossing (and some moments with nobody crossing) as frames
        plus a description, for measuring and retraining the automatic counter."""
        from ..examples import export_examples

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
        """The audit trail of this validation: every decision, who made it and when. It also
        goes to the computer's audit log (auditlog.py), which shows if it is ever edited."""
        self.state["decisions"].append({"at": _now(), "by": self._reviewer(), "action": action,
                                        **info})
        auditlog.append(action, user=self._reviewer(),
                        obj={"video": self.state["filename"],
                             "fingerprint": self.state["fingerprint"],
                             "store": self.state["store"].get("code"),
                             "validation": (self.state.get("final") or {}).get("id")},
                        before=info.pop("before", None), after=info.pop("after", None),
                        reason=info.pop("reason", None), root=self.root, **info)

    def _archive(self, reason: str) -> Path | None:
        """Keep the current check (or count by hand), and a copy of its report, before it is
        replaced."""
        if (not self.checked_work() and not self.state.get("report")
                and not self.state["manual"]["counts"]):
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
        if report.get("pdf") and Path(report["pdf"]).is_file():  # the same report, kept alike
            shutil.copy2(report["pdf"], folder / f"{stem}.pdf")
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

    @_open_only
    def start(self, confirm: bool = False) -> None:
        """Run the automatic count. Running a checked count again needs confirm: the check
        starts afresh, and the old one is kept in the history folder first."""
        with self._lock:
            if self.total_only():
                raise WizardError("This count records the total only: there is nothing to run. "
                                  "To have the tool count this footage, choose automatic "
                                  "counting instead.")
            if self.manual() and self.count_video() == self.video:
                raise WizardError("This is a manual count: there is nothing to run. Download the "
                                  "same window clean as well, and the tool counts that while you "
                                  "count this one.")
            if self._job is not None and self._job.running:
                raise WizardError("The count is already running.")
            if not self.state["cameras"]:
                raise WizardError("Draw the cameras first.")
            if unsure := self.unconfirmed():
                raise WizardError(" ".join(unsure))
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
                                 "error": None, "log": [], "video": str(self.count_video())}
            if not self.manual():  # a hand count keeps its own answers: the tool is beside it
                self.state.update(answers={}, people={}, added=[], watched=[], watch=None,
                                  report=None, decisions=[])
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

    def _gold_version(self) -> str:
        """The gold set's frozen version as it stands (gold.py): the clips the detector's own
        accuracy is measured on. Not the ground truth of this validation, which is its own."""
        from .. import gold  # gold.py uses the wizard's constants: imported only when needed

        try:
            now = gold.manifest(self.root)
        except (OSError, ValueError, KeyError) as exc:
            return f"could not be read ({exc})"
        if not now["clips"]:
            return "none yet"
        version = gold.version_of(now, self.root)
        return (f"{version}, {len(now['clips'])} clips" if version != "unreleased" else
                f"changed since its last frozen version ({len(now['clips'])} clips)")

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
        if str(model) in SMALL_MODELS:
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
            out.update(pct=100.0, message="Finished")
            if self.sealed():  # counting by hand: what the tool found waits until it is done
                out.update(message="Finished: kept until your count is done", sealed=True)
            else:
                out.update(results=self.results())
        return out

    def _twin_offset(self) -> float:
        """Seconds to add to the twin's times to land on this footage's clock: both exports
        are of the same window, so usually none."""
        from .. import retailnext as rn  # only when a twin is counted; no cycle at import time

        twin = self.twin()
        info = rn.download_info(twin) if twin is not None else None
        try:
            mine = datetime.fromisoformat(str(self._download()["start"]))
            other = datetime.fromisoformat(str((info or {})["start"]))
        except (KeyError, TypeError, ValueError):
            return 0.0
        return (other - mine).total_seconds()

    def tool_vs_hand(self) -> dict[str, Any] | None:
        """A hand count with the tool counting the twin: the two crossing by crossing
        (evaluate.score, the same matching as everywhere else), and where to look first.

        The count is the truth here; the tool is what is measured. A count made on footage
        showing RetailNext's marks can inherit its blind spots, so a crossing the tool alone
        found, in an interval where RetailNext counted no more than the person did, is
        marked: neither of them saw it, and the tool may be right."""
        if not self.manual() or self.state["job"]["status"] != "done" or self.sealed():
            return None
        dirs, offset = self.dirs(), self._twin_offset()
        counts = self.state["manual"]["counts"]
        # a count by hand is pressed after the crossing is seen: align the clocks first
        every_real = [(float(c["t"]), str(c["direction"])) for c in counts if not c.get("uncertain")]
        every_tool = [(float(c["t_seconds"]) + offset, str(c["direction"]))
                      for cam in self.state["cameras"]
                      for c in (self._read(cam, "candidates").get("candidates") or [])]
        hand_lag = evaluate.lag(every_real, every_tool, dirs)
        totals: dict[str, dict[str, int]] = {}
        rows: list[dict[str, Any]] = []
        # what each of them counted around a moment: its interval, or the whole footage when
        # the system's numbers are one total rather than one per interval
        by_interval = {r["key"]: (sum(int(v) for v in r["verified"].values()),
                                  sum(int(v) for v in r["sensor"].values()))
                       for r in self.comparison()["intervals"] if r["sensor"]}
        whole: tuple[int, int] | None = None
        totals_said = [self.state["sensor"].get(d) for d in dirs]
        if all(v is not None for v in totals_said):
            c = self.counts()
            whole = (sum(int(c["verified"][d]) for d in dirs), sum(int(v or 0) for v in totals_said))
        for cam in self.state["cameras"]:
            name = cam["sensor"]
            mine = [c for c in counts if c["camera"] == name]
            truth = [(float(c["t"]), str(c["direction"])) for c in mine if not c.get("uncertain")]
            unsure = [float(c["t"]) for c in mine if c.get("uncertain")]
            found = self._read(cam, "candidates").get("candidates") or []
            pred = [(float(c["t_seconds"]) + offset + hand_lag, str(c["direction"]))
                    for c in found if str(c["direction"]) in dirs]
            s = evaluate.score(truth, pred, dirs, unsure)
            evaluate.add(totals, s["by_direction"])
            for kind, what in (("missed", "you counted it, the tool did not"),
                               ("false", "the tool counted it, you did not"),
                               ("wrong_way", "the tool counted it the other way")):
                for at in s["at"][kind]:
                    rows.append({"key": f"{name}@{at:.2f}", "camera": name, "t": round(at, 2),
                                 "clock": self.clock(at), "kind": kind, "what": what})
        for row in rows:
            got = by_interval.get(self._interval_of(float(row["t"]))) or whole
            row["alone"] = bool(row["kind"] == "false" and got is not None and got[1] <= got[0])
            row["diagnosis"] = (self.state.get("diagnosis") or {}).get(row["key"])
        rows.sort(key=lambda r: (r["t"], r["camera"]))
        return {"video": self.count_video().name, "offset_s": round(offset, 2),
                "hand_lag_s": hand_lag, "tolerance_s": evaluate.TOLERANCE_S,
                "totals": evaluate.summary(totals) if totals else None, "rows": rows,
                "causes": CAUSES, "alone": sum(1 for r in rows if r["alone"])}

    def _interval_of(self, t: float) -> str:
        """Which of the sensor's intervals a moment of this footage falls in."""
        ivs = self.intervals()
        cs = self.state["clock_start"]
        if not cs or not ivs:
            return str(ivs[0]["key"]) if ivs else "all"
        at = datetime.fromisoformat(cs) + timedelta(seconds=t)
        for i in reversed(ivs):
            if i["start"] and datetime.fromisoformat(i["start"]) <= at:
                return str(i["key"])
        return str(ivs[0]["key"])

    @_open_only
    def set_diagnosis(self, key: str, cause: str, note: str = "") -> None:
        """Why the sensor and the count differ here, watching RetailNext's own tracker on the
        marked footage: kept with the validation, for the customer's list of things to change."""
        if cause and cause not in CAUSES:
            raise WizardError(f"unknown cause {cause!r}")
        with self._lock:
            found = dict(self.state.get("diagnosis") or {})
            if cause:
                found[key] = {"cause": cause, "note": note.strip(), "at": _now()}
            else:
                found.pop(key, None)
            self.state["diagnosis"] = found
            self._decide("diagnosed", after={"where": key, "cause": cause or None})
            self._save()

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
                                  self.dirs(), dur, audit_seed=str(self.state["fingerprint"]))
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

    @_open_only
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

    @_open_only
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

    @_open_only
    def remove_added(self, index: int) -> None:
        with self._lock:
            if not 0 <= index < len(self.state["added"]):
                raise WizardError("no such added crossing")
            removed = self.state["added"].pop(index)
            self._decide("remove_added", entry=removed)
            self._save()

    @_open_only
    def set_watch(self, mode: str | None) -> None:
        if mode not in ("doing", "done", "skipped", None):
            raise WizardError("watch mode must be doing, done or skipped")
        with self._lock:
            self.state["watch"] = mode
            self._decide("watch", mode=mode)
            self._save()

    @_open_only
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
        if self.total_only():
            t = self.state["totals"]
            verified = self.total_by_direction()
            c = {"total_only": True, "manual": False, "detected": dict.fromkeys(dirs, 0),
                 "verified": verified, "unsure": dict.fromkeys(dirs, 0), "confirmed": 0,
                 "rejected": 0, "found": 0, "not_found": 0, "added": 0, "unanswered": 0,
                 "items": 0, "watch_ranges": 0, "watch_s": 0.0, "watch_done": 0, "groups": 0,
                 "group_people": 0, "watched_pct": {}, "unwatched_s": 0.0,
                 "checked": bool(t["done"])}
            problems = [] if t["done"] else ["Counting is not finished."]
            problems += self.total_missing()
            if t["done"] and not t["whole_clip"]:
                problems.append("This total is not for the whole clip.")
            return self._completeness(c, problems)
        if self.manual():
            m = self.state["manual"]
            cams = self.manual_summary()["cameras"]
            verified = {d: sum(1 for x in m["counts"] if x["direction"] == d
                               and not x.get("uncertain")) for d in dirs}
            unsure = {d: sum(1 for x in m["counts"] if x["direction"] == d and x.get("uncertain"))
                      for d in dirs}
            c = {"manual": True, "detected": dict.fromkeys(dirs, 0), "verified": verified,
                 "unsure": unsure, "confirmed": 0, "rejected": 0, "found": 0,
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
             "not_found": 0, "added": 0, "unanswered": 0, "groups": 0, "group_people": 0,
             "asked": {"counted": 0, "possible": 0}, "answered": {"counted": 0, "possible": 0}}
        people = self.state["people"]
        items = self.check_items()
        for it in items:
            if it["kind"] == "counted":
                c["detected"][it["direction"]] += 1
            kind = "counted" if it["kind"] == "counted" else "possible"
            c["asked"][kind] += 1
            a = answers.get(it["id"])
            if a is None:
                c["unanswered"] += 1
                continue
            c["answered"][kind] += 1
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
        # what may be said: a percentage only on enough verified crossings (validation.quote)
        c["quote"] = {d: validation.quote(int(c["verified"][d]), sensor.get(d))
                      for d in self.dirs()}
        c["incomplete"] = problems
        c["status"] = "incomplete" if problems else "complete"
        c["checks"] = self.checklist(c)
        return c

    def checklist(self, c: dict[str, Any]) -> list[dict[str, Any]]:
        """Each requirement of a complete validation: passed (True), not (False), or not
        applying (None). All the tool's proposals being checked is not the same as every
        crossing being looked for: only watching the footage covers the rest."""
        rows: list[tuple[str, bool | None, str]]
        if self.total_only():
            t = self.state["totals"]
            rows = [("Counting finished", bool(t["done"]), ""),
                    ("A number for every camera and direction", not self.total_missing(), ""),
                    ("The total covers the whole clip", bool(t["whole_clip"]), "")]
        elif self.manual():
            rows = [("Counting finished", bool(self.state["manual"]["done"]), "")]
            rows += [(f"Footage of {cam} watched", pct >= MIN_WATCHED_PCT, f"{pct:.0f}%")
                     for cam, pct in c["watched_pct"].items()]
        else:
            asked, done = c["asked"], c["answered"]
            ranges = c["watch_ranges"]
            rows = [("Automatic count finished", self.state["job"]["status"] == "done", ""),
                    ("Crossings the tool counted: each checked",
                     done["counted"] == asked["counted"], f"{done['counted']} of {asked['counted']}"),
                    ("Possible misses: each checked", done["possible"] == asked["possible"],
                     f"{done['possible']} of {asked['possible']}"),
                    ("Movement the tool could not explain: watched",
                     None if not ranges else c["watch_done"] == ranges,
                     f"{c['watch_done']} of {ranges} stretches" if ranges else "there was none")]
        rows.append(("RetailNext's count entered",
                     all(self.state["sensor"].get(d) is not None for d in self.dirs()), ""))
        cov = self.coverage()
        if cov is not None and any(self.state["sensor"].get(d) is not None for d in self.dirs()):
            rows.append(("Footage covers the period RetailNext's numbers describe", cov["full"],
                         (f"{cov['footage_s'] / 60:.1f} of {cov['period_s'] / 60:g} minutes "
                          f"({cov['pct']}%)")))
        return [{"name": n, "ok": ok, "detail": d} for n, ok, d in rows]

    def unsure_rows(self) -> list[dict[str, Any]]:
        """Crossings the checker could not decide: listed in the report, never counted."""
        if self.total_only():
            return []
        if self.manual():
            return [{"t": c["t"], "camera": c["camera"],
                     "picture": self._camera(c["camera"])["picture"], "direction": c["direction"],
                     "point": None, "found": "Unclear to the person counting: not counted"
                     + (f" ({c['note']})" if c.get("note") else "")}
                    for c in self.state["manual"]["counts"]
                    if c.get("uncertain") and c["direction"] in self.dirs()]
        answers = self.state["answers"]
        return [{"t": it["t"], "camera": it["camera"], "picture": it["picture"],
                 "direction": it["direction"], "point": it["point"],
                 "found": "Unclear to the checker: not counted"}
                for it in self.check_items() if answers.get(it["id"]) == "unsure"]

    def verified_rows(self) -> list[dict[str, Any]]:
        """Every verified crossing, with its moment. A total-only count has none: it says how
        many crossed, never when, so there is nothing here to match anything against."""
        if self.total_only():
            return []
        if self.manual():
            rows = [{"t": c["t"], "camera": c["camera"],
                     "picture": self._camera(c["camera"])["picture"], "direction": c["direction"],
                     "point": None,
                     "found": "Counted by hand" + (f": {c['note']}" if c.get("note") else "")}
                    for c in self.state["manual"]["counts"]
                    if c["direction"] in self.dirs() and not c.get("uncertain")]
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
            twin = self.twin()
            return {**self.state, "dirs": self.dirs(), "counts": self.counts(), "fps": self.fps,
                    "twin": twin.name if twin is not None else None,
                    "counting_video": self.count_video().name, "sealed": self.sealed(),
                    "run_dir": str(self.run_dir), "history": self.history(),
                    "intervals": self.intervals(), "comparison": self.comparison(),
                    "detectors": [{"model": m, **DETECTOR_CHOICES[m], "unavailable": unavailable(m)}
                                  for m in MODELS]}

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
        if self.manual() or self.total_only():
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
        elif self.total_only():
            t = self.state["totals"]
            each = ", ".join(f"{cam['sensor']} "
                             + " and ".join(f"{cam['counts'][d]} {LABELS[d].lower()}"
                                            for d in self.dirs())
                             for cam in self.total_summary()["cameras"])
            method = [
                method[0],
                ("Counted by hand as a total: a person watched the footage and pressed a key "
                 "for each person, keeping the number only. The moment each person crossed was "
                 "not recorded. No automatic detection was used."),
                f"Counted: {each}.",
                ("Because no moment was recorded, this count can be set against the system's "
                 "number only as one total against another. It cannot show which crossings the "
                 "system found and which it missed: an over-count and an under-count in the "
                 "same period cancel out in a total and would look like agreement."),
                method[-1],
            ]
            if t["notes"]:
                method.insert(3, f"Note from the person counting: {t['notes']}")
        scope = sampling.scope(st.get("sampling"), f"{start:%H:%M}–{end:%H:%M} on "
                               f"{start:%d/%m/%Y}" if start and end else "")
        method.insert(1, scope)
        levels = validation.by_level(self.comparison()["rows"])
        system = (st.get("sensor_source") or {}).get("system") or "RetailNext"
        sample_notes = []  # beside the headline number: how much it rests on
        for d in [] if c["incomplete"] else self.dirs():
            q = c["quote"][d]
            if q["system"] is None:
                continue
            sample_notes.append(
                f"{LABELS[d]}: from {q['truth']} verified crossings in one window, so no range is "
                f"given (one window cannot show how the result would move at another time)."
                if q["rate"] else
                f"{LABELS[d]}: verified {q['truth']}, {system} counted {q['system']}: "
                f"{q['words']}. {q['reason']}.")
        rules, said = self.state["rules"], {"count": "counted", "exclude": "not counted"}
        spec = self.state["manual"].get("specification") or GROUND_TRUTH_SPEC
        method.insert(len(method) - 1,
                      (f"Who was counted: children {said[rules['children']]}, staff "
                       f"{said[rules['staff']]}." if self.total_only() else
                       f"Crossings as defined by CrossingCount's Ground Truth Specification "
                       f"v{spec}: children {said[rules['children']]}, staff "
                       f"{said[rules['staff']]}."))
        foot = self.footage()
        if not foot["clean"]:
            method.insert(len(method) - 1,
                          f"{'Counted' if self.manual() or self.total_only() else 'Checked'} "
                          f"on footage showing "
                          f"RetailNext's own tracks and counts ({'; '.join(foot['why_marked'])}), "
                          f"which can sway a count towards the sensor's: this count is not "
                          f"independent of the sensor.")
        if lines := self.correspondence():
            how_lines = "; ".join(f"{cam} {x['words']}" for cam, x in lines.items())
            weakest = min(x["strength"] for x in lines.values())
            method.insert(len(method) - 1, f"Counting lines: {how_lines}." + (
                " A line drawn by eye is the weakest evidence that it is where the sensor counts: "
                "part of any difference from RetailNext may be the line, not the sensor."
                if weakest < 2 else ""))
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
        word = {True: "pass", False: "NOT MET", None: "does not apply"}
        method.append("Validation checks: " + "; ".join(
            k["name"] + (f" ({k['detail']})" if k["detail"] else "") + f": {word[k['ok']]}"
            for k in c["checks"]) + ".")
        method.append("Sensor accuracy compares RetailNext's count with the count verified here: "
                      "it shows how close the sensor came, not how well the automatic counter "
                      "did.")
        if self.total_only():
            method.append("This is a comparison of totals. It is not a crossing-by-crossing "
                          "result: no recall, precision or missed-crossing rate can be worked "
                          "out from it, and none is given.")
        method.append(f"Report made with CrossingCount {app_version()}, {__copyright__}.")
        # page 1, for someone who reads nothing else: the result in people and which way,
        # what it rests on, and what could not be known. The method above stays as it is.
        opening = sampling.when(st.get("sampling"), f"{start:%H:%M} to {end:%H:%M} on "
                                f"{start:%d/%m/%Y}" if start and end else "")
        headline = [] if c["incomplete"] else [  # when and where once, on the first sentence
            validation.window_headline(system, PEOPLE[d], int(st["sensor"][d]), c["verified"][d],
                                       c["unsure"][d], "" if k else opening)
            for k, d in enumerate(d for d in self.dirs() if st["sensor"].get(d) is not None)]
        caveats = list(dict.fromkeys((st.get("sensor_source") or {}).get("warnings") or []))
        if short := self.coverage_words():
            caveats.append(short)
        if not foot["clean"]:
            caveats.append(f"Not independent of {system}: the footage shows its own tracks and "
                           f"counts ({'; '.join(foot['why_marked'])}).")
        if lines and min(x["strength"] for x in lines.values()) < 2:
            caveats.append(f"A counting line was drawn by eye: part of any difference from "
                           f"{system} may be where the line is, not the sensor.")
        identity = {
            "validation_id": (st.get("final") or {}).get("id"),
            "sampling": sampling.label(st.get("sampling")),
            "specification": ("Total only: no moment recorded for any crossing"
                              if self.total_only() else f"Ground Truth Specification v{spec}"),
            "gold_set": self._gold_version(),
            "footage": ("clean: no RetailNext marks" + (", checked in the picture"
                                                       if foot["checked_in_picture"] else "")
                        + f"; {foot['obtained_words']}") if foot["clean"]
            else f"marked: {'; '.join(foot['why_marked'])}",
        }
        return {
            "headline": headline, "identity": identity, "caveats": caveats,
            "count_label": "MANUAL COUNT" if self.manual() or self.total_only()
            else "VERIFIED COUNT",
            "frames_title": ("VALIDATION FRAMES — BUSIEST MOMENT"
                             if self.manual() or self.total_only()
                             else "VALIDATION FRAMES — AUTOMATED DETECTION OVERLAY"),
            "store_name": store["name"], "store_code": store["code"],
            "location": store["location"] or "Entrance",
            "report_date": store["report_date"] or datetime.now().astimezone().strftime("%d/%m/%Y"),
            "captured_date": start.strftime("%d/%m/%Y") if start else "",
            "time_range": f"{start:%H:%M}-{end:%H:%M}" if start and end else "",
            "directions": [{"key": d, "label": LABELS[d], "verified": c["verified"][d],
                            "unsure": c["unsure"][d], "system": st["sensor"][d],
                            "rate": c["quote"][d]["rate"],
                            "accuracy": c["accuracy"][d]["accuracy_pct"],
                            "accuracy_range": c["accuracy_range"][d]} for d in self.dirs()],
            "complete": not c["incomplete"], "incomplete": c["incomplete"],
            "breakdown": self._breakdown(),
            "frame": str(frame), "frame_caption": caption,
            "crossings": [{"n": n, "time": self.clock(r["t"]), "camera": r["camera"],
                           "direction": LABELS[r["direction"]], "found": r["found"]}
                          for n, r in [*zip(row_numbers(rows), rows), *(("?", u) for u in unsure)]],
            # a total has no list of crossings to print, and saying none was verified would be
            # wrong: they were counted, only never placed in time
            "crossings_note": (
                "Counted as a total only: "
                + ", ".join(f"{c['verified'][d]} {PEOPLE[d]}" for d in self.dirs())
                + ". No moment was recorded for any of them, so there is no list of crossings "
                  "here and none can be matched against a system's own."
                if self.total_only() else None),
            "thumbs": thumbs, "method": method, "scope": scope, "sample_notes": sample_notes,
            "levels": {"text": validation.level_sentence(levels, system),
                       "rows": [{"level": k, **m, **({} if m["truth"] >= validation.MIN_VERIFIED_FOR_PCT
                                                     else {"bias_pct": None, "wape_pct": None})}
                                for k, m in levels.items()],
                       "footage": self.traffic(c)},
        }

    @_open_only
    def make_report(self, logo: Path | None = None) -> Path:
        with self._lock:
            if unsure := self.unconfirmed():  # whose crossings are these? before anything else
                raise WizardError(" ".join(unsure))
            c = self.counts()
            if self.total_only():
                if not c["checked"]:
                    raise WizardError("Save the total first.")
                if missing := self.total_missing():
                    raise WizardError(missing[0])
            elif self.manual():
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
            pdf = build_pdf(data, out.with_suffix(".pdf"), logo)
            # what the report says, kept: finalising renders it again with the validation's ID
            kept = out.with_suffix(".report.json")
            write_json_atomic(kept, {"logo": str(logo) if logo else None, "data": data})
            self.state["report"] = {"path": str(out), "pdf": str(pdf), "data": str(kept),
                                    "made_at": _now()}
            self.state["result"] = self.result()  # for putting validations together
            self._decide("report_made", after={"file": out.name,
                                               "sha256": provenance.file_sha256(out),
                                               "pdf_sha256": provenance.file_sha256(pdf)})
            self._save()
            return out
