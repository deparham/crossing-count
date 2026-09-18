"""What a check puts to a person, and the small shared pieces the wizard counts with.

Nothing here touches a validation's state: given one camera's automatic results, it says
what a person is asked about (every counted crossing, the likely misses, a seeded sample
of the rule's other rejections) and what is left to watch. The wizard, the benchmark and
the gold set all use these, so they ask the same questions of the same footage.
"""

from __future__ import annotations

import importlib.util
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from ..candidates import MISS_KIND
from ..export import sensor_accuracy

VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".mkv", ".avi")
DIRECTIONS = ("in", "out")
CHOICES = {"in": ["in"], "out": ["out"], "both": ["in", "out"]}
LABELS = {"in": "Traffic In", "out": "Traffic Out"}
PEOPLE = {"in": "people coming in", "out": "people going out"}  # in a sentence
# The detector's weights, best first: the wizard's default is MODELS[0]. YOLO26 sees more
# people per frame than YOLO11 at the same speed on this footage (16 Sep 2026, CN-123
# 11:30, two cameras); whether that counts better is not known until a clip is counted by
# hand, and every result records which weights produced it.
RFDETR = "rfdetr-large"  # RF-DETR Large on the whole picture (detector.RfDetrBackbone)
RFDETR_WEIGHTS = "rf-detr-large-2026.pth"
MODELS = ("yolo26m.pt", "yolo26s.pt", "yolo11m.pt", "yolo11s.pt", RFDETR)
SMALL_MODELS = ("yolo26s.pt", "yolo11s.pt")  # faster, and they miss more people
# What the page offers. Speeds were measured on an M-series Mac on 16 Sep 2026 (CN-123,
# two cameras, all footage active), per camera for 15 minutes of footage; none is known
# for the ones without. RF-DETR runs on the whole picture only: inside the de-rotated
# pipeline it resizes each of ~30 crops a frame to 704 px, about 8 hours a camera.
DETECTOR_CHOICES: dict[str, dict[str, str | None]] = {
    "yolo26m.pt": {"label": "Accurate: YOLO26 (the default)",
                   "speed": "about 30 minutes a camera for 15 minutes of footage"},
    "yolo26s.pt": {"label": "Fast: YOLO26 small (misses more people)", "speed": None},
    "yolo11m.pt": {"label": "Accurate: YOLO11 (what earlier counts used)",
                   "speed": "about 30 minutes a camera for 15 minutes of footage"},
    "yolo11s.pt": {"label": "Fast: YOLO11 small (misses more people)",
                   "speed": "about 9 minutes a camera for 15 minutes of footage"},
    RFDETR: {"label": "RF-DETR Large, whole picture (Apache-2.0; not yet proven by a hand count)",
             "speed": "about 6 minutes a camera for 15 minutes of footage"},
}
TWIN_WINDOW_S = 2.0  # same direction this close on another camera: maybe one person seen twice
# Rule rejections offered to the checker. Measured on checked clips (11:30 CN-123, YD-612):
# "never touched the filter zone" and "out and back on one track" were real about half the
# time or more; exits without the mask 3 in 7; entries lost before the mask 0 in 8; tracks
# broken at the line 9 in 94. U-turns (0 in 2) stay rejected.
POSSIBLE_REASONS = ("no_filter", "returned_same_track", "outward_no_mask", "pending_expired",
                    "pending_at_eof", "pending_at_range_end")
PROMPT_PRIORITY = {"no_filter": 0, "returned_same_track": 0, "outward_no_mask": 0,
                   "pending_expired": 1, "pending_at_eof": 1, "pending_at_range_end": 1,
                   "lost": 2, "audit": 3}
# The rule's other rejections are not trusted: a seeded sample of them is put to the checker
# as well, so the rule itself is audited. Asked last, after the likely misses.
AUDIT_SHARE = 0.25
AUDIT_MIN = 5
CLIP_BEFORE_S = 2.5
CLIP_AFTER_S = 1.5
MIN_WATCHED_PCT = 99.0  # a hand count that watched less of a camera's footage is incomplete
# Why the sensor's count and the person's differ, seen on RetailNext's own marked footage:
# what a customer can act on (the camera, its line, its settings).
CAUSES = {
    "no_track": "RetailNext never tracked the person",
    "track_lost": "RetailNext's track was lost at the line",
    "line_position": "RetailNext's line is somewhere else",
    "counted_twice": "RetailNext counted the same person twice",
    "wrong_way": "RetailNext counted the other direction",
    "height_filter": "RetailNext left the person out (a child, or its height filter)",
    "view": "The camera cannot see it: glare, an obstruction, or its angle",
    "our_miss": "The tool's mistake, not RetailNext's",
    "other": "Something else (say what)",
}
SHORTFALL_S = 30.0  # footage shorter than the sensor's intervals by more than this is said so
GROUND_TRUTH_SPEC = "1.3"  # docs/GROUND_TRUTH_SPECIFICATION.md: what a crossing is
RULE_CHOICES = ("count", "exclude")  # what counts as a person: children, staff (spec section 4)
MAX_GROUP = 9  # people one answer can count, when a group crosses together
FOUND = {  # how each verified crossing came to be counted, for the report
    "detected": "Detected, confirmed",
    "lost": "Track lost at line, confirmed",
    "added": "Added by the checker",
    "audit": "Rejected by the rule, checked as a sample, restored",
}


def prompt_priority(why: str) -> int:
    """Possible misses most often real come first; tracks broken at the line last."""
    return PROMPT_PRIORITY.get(why, 1)


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
                 unexplained: dict[str, Any], dirs: list[str], duration: float,
                 audit_seed: str | None = None) -> list[dict[str, Any]]:
    """What one camera's count asks a person to check: every counted crossing, and every
    possible miss (rule rejections that are often real, tracks lost at the line).

    With an audit_seed, a seeded sample of the rule's *other* rejections is asked about too,
    so the counting rule is audited rather than trusted; the same seed always picks the same
    sample. Scoring replays pass no seed, so a score is never changed by the audit.
    """
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
    if audit_seed is not None:
        rest = [d for d in discarded.get("discarded", [])
                if d.get("reason") not in POSSIBLE_REASONS
                and (d.get("crossings") or [{}])[0].get("direction") in dirs]
        rng = random.Random(f"{audit_seed}:{camera}")
        share = min(len(rest), max(AUDIT_MIN, round(AUDIT_SHARE * len(rest))))
        for d in sorted(rng.sample(rest, share), key=lambda d: float(d["crossings"][0]["t"])):
            first = d["crossings"][0]
            t = float(first["t"])
            items.append({**base, "id": d["id"], "kind": "possible", "why": "audit", "t": t,
                          "direction": first["direction"], "clip": clip(t, t + 1.5),
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


def unavailable(model: str) -> str | None:
    """Why a detector cannot run in this copy of the app, or None when it can."""
    if model != RFDETR:
        return None
    if importlib.util.find_spec("rfdetr") is None:
        return ("RF-DETR is not installed in this copy: the installed app leaves it out until "
                "it has proved itself against hand counts. From the project folder, run "
                "uv sync --group detectors.")
    from ..detector import weights_path

    try:
        weights_path(RFDETR_WEIGHTS)
    except FileNotFoundError:
        return f"RF-DETR's weights are not in models/ ({RFDETR_WEIGHTS}; see the README)."
    return None
