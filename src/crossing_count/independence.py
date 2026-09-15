"""Was the footage a count was made on clean, and how do we know?

A count is independent of the sensor only if the person counting could not see the
sensor's own work: RetailNext's burned-in counting line, track dots and height bubbles.
Four things say whether footage shows them, and any one is enough (a flag is never
trusted over the picture):

    in the picture   RetailNext's thin blue lines found on the people-free picture of a
                     camera (overlay.counting_overlay_evidence)
    how obtained     a RetailNext download remembers whether its marks were asked for
    its name         "Export - 392 marked - ..." (the name a marked download gets)
    what was said    the person's answer on the wizard's marks step

This applies to every count, automatic or by hand: a person checking the tool's
crossings on marked footage sees RetailNext's tracks too. Results made before this check
existed are audited (audit()) and, where the footage was marked, reclassified in a
registry beside the audit log; results themselves are never edited.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from . import auditlog
from . import layout as lay
from . import overlay as ov
from . import retailnext as rn
from . import video as vid
from .util import write_json_atomic

OBTAINED = {"download_clean": "downloaded from RetailNext without its marks",
            "download_marked": "downloaded from RetailNext with its marks",
            "by_hand": "a file chosen by hand"}
CLEAN_PATH = ("Count on clean footage: on the first page, Get footage from RetailNext with "
              "Count: By hand downloads it without RetailNext's marks.")


def name_says_marked(filename: str) -> bool:
    m = rn._DOWNLOAD_NAME.match(Path(filename).name)
    return bool(m and m["marks"])


def detect(video: Path) -> list[dict[str, Any]]:
    """Per camera picture: are RetailNext's lines on its people-free picture? (As the setup
    page's Setup.marks(), for footage the page has not opened.)"""
    audit = vid.audit_timebase(video)
    median = vid.median_of_video(video, audit.duration_s, interval_s=audit.median_interval_s)
    return [ov.counting_overlay_evidence(t.crop(median)[t.header_px:])
            for t in lay.detect_tiles_in(median)]


def facts(filename: str, download: Mapping[str, Any] | None, said: bool | None,
          detected: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    """Is the footage clean, why not, and how it was obtained."""
    why = []
    seen = [i + 1 for i, d in enumerate(detected or []) if d.get("marks")]
    if seen:
        why.append(f"RetailNext's lines are visible in picture {', '.join(map(str, seen))}")
    if download and download.get("marks"):
        why.append("it was downloaded from RetailNext with its marks")
    if name_says_marked(filename):
        why.append("its name says it shows RetailNext's marks")
    if said:
        why.append("the person counting said it shows RetailNext's marks")
    key = ("by_hand" if not download or "marks" not in download
           else "download_marked" if download.get("marks") else "download_clean")
    return {"clean": not why, "why_marked": why, "obtained": key, "obtained_words": OBTAINED[key],
            "checked_in_picture": detected is not None,
            "pictures": [{"picture": i + 1, "marks": bool(d.get("marks")),
                          "line_px": d.get("length_px")} for i, d in enumerate(detected or [])]}


def of_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """A wizard state's footage facts."""
    return facts(str(state.get("filename") or ""), state.get("retailnext"), state.get("marks"),
                 state.get("marks_detected"))


# ---- earlier results: audit, reclassify --------------------------------------------------------

def registry_path(root: Path) -> Path:
    return root / "audit" / "reclassified.json"


def registry(root: Path | None) -> dict[str, dict[str, Any]]:
    """Results reclassified as counted on marked footage, by the footage's fingerprint."""
    if root is None:
        return {}
    try:
        data = json.loads(registry_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def apply(result: Mapping[str, Any], reg: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """A result as reclassified (a copy): marked, with the reason."""
    entry = reg.get(str(result.get("fingerprint")))
    if not entry or result.get("marked"):
        return dict(result)
    return {**result, "marked": True, "reclassified": entry}


def audit(root: Path, look: bool = True) -> list[dict[str, Any]]:
    """Every result on this computer that says it was counted on clean footage although
    the footage shows RetailNext's marks. With look, a video still on this computer is
    checked in the picture as well."""
    found = []
    reg = registry(root)
    for p in sorted((root / "runs").glob("*/wizard/state.json")):
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        res = st.get("result")
        if not isinstance(res, dict) or res.get("marked") or str(st.get("fingerprint")) in reg:
            continue
        detected = st.get("marks_detected")
        if detected is None and look and Path(str(st.get("video") or "")).is_file():
            try:
                detected = detect(Path(st["video"]))
            except (OSError, ValueError):
                detected = None
        f = facts(str(st.get("filename") or ""), st.get("retailnext"), st.get("marks"), detected)
        if not f["clean"]:
            found.append({"fingerprint": str(st.get("fingerprint")), "filename": st.get("filename"),
                          "store": (st.get("store") or {}).get("code"), "mode": st.get("mode"),
                          "finalised": (st.get("final") or {}).get("id"), "why": f["why_marked"]})
    return found


def reclassify(root: Path, findings: Sequence[Mapping[str, Any]], by: str = "") -> dict[str, Any]:
    """Record audit findings as counted on marked footage: kept out of every comparison with
    the system (validation.exclusions), with the reason. Logged; results stay as they are."""
    reg = registry(root)
    at = datetime.now().astimezone().isoformat(timespec="seconds")
    for f in findings:
        entry = {"why": list(f["why"]), "at": at, "by": by, "filename": f.get("filename"),
                 "finalised": f.get("finalised")}
        reg[str(f["fingerprint"])] = entry
        auditlog.append("reclassified_marked", user=by, obj={"video": f.get("filename"),
                                                            "validation": f.get("finalised")},
                        before={"marked": False}, after={"marked": True},
                        reason="; ".join(f["why"]), root=root)
    registry_path(root).parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(registry_path(root), reg)
    return reg
