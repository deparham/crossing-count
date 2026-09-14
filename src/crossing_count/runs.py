"""Validation runs: a finished validation, finalised under an ID and never changed again.

Finalising gives the validation an ID, CC-VAL-<year>-<computer>-<number> (the computer's
four characters keep IDs from different computers of a team apart), and writes a folder
<data>/validations/<ID>/:

    manifest.json    everything needed to know what was compared with what, and how:
                     software, engines, ground-truth rules, footage checksum, lines drawn,
                     the system's numbers and their source, detector, weights and
                     settings, the computer, the audit log's latest hash, and the SHA-256
                     of every other file here
    result.json      the result, as validation.py reads it
    intervals.csv    verified against the system, interval by interval
    crossings.csv    every verified crossing (and every unsure one, marked not counted)
    decisions.json   the validation's own decision log
    <report>.pptx    the report as it was made

The files are made read-only and verify() checks them against the manifest. The wizard
refuses any change to a finalised validation: a correction is a new version, finalised
under a new ID that names the one it supersedes, and the old folder stays as it was.
With a shared folder set, the run is copied to <shared>/runs/<ID>/.
"""

from __future__ import annotations

import csv
import getpass
import hashlib
import io
import json
import platform
import re
import shutil
import stat
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import auditlog, evaluate, provenance, validation

if TYPE_CHECKING:
    from .wizard import Wizard

PREFIX = "CC-VAL"
SCHEMA = "validation-manifest/1"
ID_RE = re.compile(rf"^{PREFIX}-(\d{{4}})-([0-9A-F]{{4}})-(\d{{6}})$")


class RunError(Exception):
    """A run that cannot be finalised or read, said plainly."""


def computer_tag() -> str:
    """Four characters that tell this computer's runs from a colleague's."""
    try:
        who = getpass.getuser()
    except (OSError, KeyError):
        who = ""
    return hashlib.sha256(f"{platform.node()}|{who}".encode()).hexdigest()[:4].upper()


def runs_dir(root: Path) -> Path:
    return root / "validations"


def allocate(root: Path, year: int) -> tuple[str, Path]:
    """The next ID for this computer and year, its folder created (never an existing one)."""
    folder = runs_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    tag = computer_tag()
    used = [int(m[3]) for p in folder.iterdir()
            if (m := ID_RE.match(p.name)) and int(m[1]) == year and m[2] == tag]
    n = max(used, default=0) + 1
    while True:
        vid = f"{PREFIX}-{year}-{tag}-{n:06d}"
        try:
            (folder / vid).mkdir()
        except FileExistsError:
            n += 1
            continue
        return vid, folder / vid


def _canonical_sha(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                     default=str).encode("utf-8")).hexdigest()


def _csv(rows: list[list[Any]]) -> str:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue()


def write(w: Wizard, root: Path, shared: Path | None = None) -> dict[str, Any]:
    """Finalise: the run's folder, its files and manifest, read-only. Returns its summary."""
    from .wizard import TWIN_WINDOW_S

    st = w.state
    report = Path(str((st.get("report") or {}).get("path") or ""))
    if not report.is_file():
        raise RunError("Make the report first: a finalised validation keeps it.")
    now = datetime.now().astimezone()
    vid, folder = allocate(root, now.year)
    result = w.result()
    cams = [c["sensor"] for c in st["cameras"]]
    files: dict[str, str] = {}

    def put(name: str, text: str) -> None:
        (folder / name).write_text(text, encoding="utf-8")

    put("result.json", json.dumps(result, indent=2, ensure_ascii=False, default=str))
    put("intervals.csv", _csv(
        [["validation_id", "interval", "start", "direction", "verified", "system", "error"]]
        + [[vid, r["interval"], r["start"], r["direction"], r["truth"], r["system"],
            int(r["system"]) - int(r["truth"])] for r in result["rows"]]))
    counted = [[vid, w.clock(float(r["t"])), round(float(r["t"]), 2), r["camera"], r["direction"],
                int(r.get("people", 1)), r["found"], "counted"] for r in w.verified_rows()]
    unsure = [[vid, w.clock(float(r["t"])), round(float(r["t"]), 2), r["camera"], r["direction"],
               1, r["found"], "unsure: not counted"] for r in w.unsure_rows()]
    put("crossings.csv", _csv([["validation_id", "clock", "seconds", "camera", "direction",
                                "people", "how_found", "status"], *counted, *unsure]))
    put("decisions.json", json.dumps(st.get("decisions", []), indent=2, ensure_ascii=False))
    shutil.copy2(report, folder / report.name)
    for p in sorted(folder.iterdir()):
        files[p.name] = provenance.file_sha256(p)
    geometry = w.geometry()
    video_sha = provenance.file_sha256(w.video) if w.video.is_file() else None
    src = dict(st.get("sensor_source") or {})
    src.pop("numbers", None)
    numbers = w._sensor_numbers()
    manifest = {
        "schema": SCHEMA, "validation_id": vid, "supersedes": st.get("supersedes"),
        "created_at": st.get("created_at"), "finalised_at": now.isoformat(timespec="seconds"),
        "operator": st["store"].get("operator"), "login": auditlog._login(),
        "software": provenance.software(), "hardware": provenance.hardware(),
        "engines": {"count_validation": validation.ENGINE,
                    "crossing_evaluation": evaluate.ENGINE, "matching": evaluate.MATCHING},
        "ground_truth": {"specification": result["specification"], "rules": st.get("rules"),
                         "mode": result["mode"], "marked_footage": result["marked"],
                         "check_same_person_window_s": TWIN_WINDOW_S},
        "store": {k: st["store"].get(k) for k in ("code", "name", "location", "report_date")},
        "footage": {"filename": st["filename"], "fingerprint": st["fingerprint"],
                    "sha256": video_sha, "duration_s": st["duration_s"],
                    "clock_start": st["clock_start"], "clock_end": st.get("clock_end"),
                    "tz": st.get("tz")},
        "cameras": [{"sensor": c["sensor"], "picture": c["picture"], "tile": c.get("tile"),
                     "config": c.get("config"), "line": geometry.get(c["sensor"], {}).get("line"),
                     "mask": geometry.get(c["sensor"], {}).get("mask")} for c in st["cameras"]],
        "system_under_test": {**src, "system": result["system"], "source": result["source"],
                              "numbers": numbers, "numbers_sha256": _canonical_sha(numbers)},
        "detection": ({"model": st.get("model"),
                       "cameras": provenance.detection(w.run_dir, cams)}
                      if result["mode"] != "manual" else None),
        "status": result["status"], "incomplete": result["incomplete"],
        "checks": result["checks"], "metrics": result["metrics"],
        "audit_log_head": auditlog.head(root),
        "files": files,
    }
    manifest_text = json.dumps(manifest, indent=2, ensure_ascii=False, default=str)
    put("manifest.json", manifest_text)
    for p in folder.iterdir():  # read-only: a finalised run is never edited in place
        p.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    copied = False
    if shared is not None:
        try:
            shutil.copytree(folder, shared / "runs" / vid)
            copied = True
        except OSError:
            copied = False
    return {"validation_id": vid, "folder": str(folder), "finalised_at": manifest["finalised_at"],
            "manifest_sha256": hashlib.sha256(manifest_text.encode("utf-8")).hexdigest(),
            "files": sorted([*files, "manifest.json"]), "shared": copied}


def read(folder: Path) -> dict[str, Any]:
    try:
        data = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RunError(f"{folder.name}: no readable manifest ({e})") from None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise RunError(f"{folder.name}: not a validation manifest")
    return data


def verify(folder: Path, root: Path | None = None) -> dict[str, Any]:
    """Are the run's files still exactly as finalised, and is its audit-log hash still in the
    log?"""
    m = read(folder)
    changed, missing = [], []
    for name, sha in m["files"].items():
        p = folder / name
        if not p.is_file():
            missing.append(name)
        elif hashlib.sha256(p.read_bytes()).hexdigest() != sha:
            changed.append(name)
    head = m.get("audit_log_head")
    in_log = auditlog.contains(head, root) if head and head != auditlog.GENESIS else None
    return {"intact": not changed and not missing, "changed": changed, "missing": missing,
            "audit_log_ok": in_log}


def listing(root: Path) -> list[dict[str, Any]]:
    """Every finalised run on this computer, newest first."""
    out: list[dict[str, Any]] = []
    folder = runs_dir(root)
    if not folder.is_dir():
        return out
    for p in sorted((p for p in folder.iterdir() if ID_RE.match(p.name)), reverse=True):
        try:
            m = read(p)
            check = verify(p, root)
        except RunError as e:
            out.append({"validation_id": p.name, "problem": str(e)})
            continue
        met: dict[str, Any] = m.get("metrics") or {}
        tot: dict[str, Any] = (met.get("total") or next(iter(met.values()), {})) if met else {}
        out.append({"validation_id": m["validation_id"], "supersedes": m.get("supersedes"),
                    "finalised_at": m["finalised_at"], "operator": m.get("operator"),
                    "store": m["store"], "clock_start": m["footage"]["clock_start"],
                    "cameras": [c["sensor"] for c in m["cameras"]],
                    "system": m["system_under_test"]["system"], "status": m["status"],
                    "mode": m["ground_truth"]["mode"], "folder": str(p),
                    "verified": tot.get("truth") if tot else None,
                    "system_count": tot.get("system") if tot else None, **check})
    return out
