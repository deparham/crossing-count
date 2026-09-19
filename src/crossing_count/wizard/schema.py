"""Reading a validation's state file: what must be in it, what is filled in, and saying
plainly when one is wrong.

A state file is JSON tagged with a schema version. It is read far more often than it is
written, sometimes by a version of the app newer or older than the one that wrote it, so:

  * a file that cannot be read says **which file** and **what is wrong with it**, and is
    never rewritten or repaired in place: a count is only ever lost by someone deleting it;
  * what a later version added is filled in with its default, and the caller is told what
    was filled in (`migrate`);
  * keys this version does not know are kept exactly as they are, so an older app cannot
    quietly drop what a newer one wrote;
  * a file from a *newer* schema is refused rather than half-understood.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .items import MODELS

SCHEMA = "wizard/1"
FAMILY = "wizard"
# What makes a state file this video's count. Without these it is not one.
REQUIRED = ("fingerprint", "filename", "duration_s")
# Everything a count needs but an older file may not have. Each is filled in on load, and
# new entries are added here rather than in the wizard.
DEFAULTS: dict[str, Any] = {
    "cameras": [],
    "direction": None,
    "sensor": {},
    "model": MODELS[0],
    "store": {"name": "", "code": "", "location": "Entrance", "report_date": "", "operator": ""},
    "job": {"status": "idle", "started_at": None, "finished_at": None, "error": None, "log": []},
    "answers": {},
    "added": [],
    "watched": [],
    "watch": None,
    "report": None,
    "version": 0,
    "mode": None,
    "marks": None,
    "marks_detected": None,
    "examples": None,
    "people": {},
    "decisions": [],
    "sensor_intervals": {},
    "sensor_cameras": {},
    "sensor_source": None,
    "sampling": None,
    "rules": {"children": "count", "staff": "count"},
    "manual": {"counts": [], "watched": {}, "positions": {}, "done": False, "next_id": 1},
    # a count of the total only: a number per camera and direction, no moments. Deliberately
    # without a time per press, so it can never be mistaken for a count of crossings.
    "totals": {"by_camera": {}, "notes": "", "done": False, "whole_clip": False, "actions": []},
}


class StateError(ValueError):
    """A state file that cannot be used, said plainly: which file, and what is wrong."""


def version_of(state: dict[str, Any]) -> tuple[str, int]:
    """("wizard", 1) from "wizard/1"; ("", 0) when a file predates versioning."""
    raw = str(state.get("schema") or "")
    family, _, number = raw.partition("/")
    return family, int(number) if number.isdigit() else 0


def migrate(state: dict[str, Any]) -> list[str]:
    """Fill in what later versions of the wizard added. Returns the names filled in.

    Nothing is removed and nothing is overwritten: a key already there is left alone,
    whatever its value, and keys this version does not know are untouched.
    """
    filled: list[str] = []
    for key, value in DEFAULTS.items():
        if key not in state:
            state[key] = copy.deepcopy(value)
            filled.append(key)
    store = state.get("store")
    if isinstance(store, dict):
        for key, value in DEFAULTS["store"].items():
            if key not in store:
                store[key] = value
                filled.append(f"store.{key}")
    for block in ("manual", "totals"):
        inner = state.get(block)
        if isinstance(inner, dict):
            for key, value in DEFAULTS[block].items():
                if key not in inner:
                    inner[key] = copy.deepcopy(value)
                    filled.append(f"{block}.{key}")
    state["schema"] = SCHEMA
    return filled


def load_state(path: Path, fingerprint: str) -> tuple[dict[str, Any], list[str]]:
    """One video's count, ready to use, with what had to be filled in.

    Raises StateError, naming the file, rather than failing somewhere deeper later.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateError(f"{path} could not be read ({exc}).") from None
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise StateError(
            f"{path} is not readable as JSON ({exc}). Nothing in it has been changed: move "
            f"the file aside to start this count again, or repair the file if the count in "
            f"it matters.") from None
    if not isinstance(data, dict):
        raise StateError(f"{path} does not hold a count: its contents are a "
                         f"{type(data).__name__}, not an object.")
    family, number = version_of(data)
    if family and family != FAMILY:
        raise StateError(f"{path} is a {data.get('schema')} file, not a count's state.")
    if number > int(SCHEMA.split('/')[1]):
        raise StateError(
            f"{path} was written by a newer version of CrossingCount ({data.get('schema')}; "
            f"this one reads {SCHEMA}). Update the app rather than opening it here: an older "
            f"version could drop what the newer one saved.")
    missing = [k for k in REQUIRED if k not in data]
    if missing:
        raise StateError(f"{path} is missing {', '.join(missing)}, so it cannot be a count's "
                         f"state file. Move it aside to start this count again.")
    if data.get("fingerprint") != fingerprint:
        raise StateError(f"{path} belongs to a different video with the same name; move that "
                         f"folder away to start again.")
    return data, migrate(data)
