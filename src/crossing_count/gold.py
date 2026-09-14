"""The gold set: clips a person counted in full, kept to measure the automatic count.

A gold clip is a count by hand in the wizard (Manual) on clean footage that watched at
least MIN_WATCHED_PCT of every counted camera's footage: every crossing in it was looked
for, not only those the tool pointed at, and nothing the sensor drew could sway it.
("Every crossing the tool proposed was checked" is a different thing and never makes a
gold clip.) Each clip is one JSON file under gold/<dataset>/, written only from people's
counts, never by a model. See docs/DATASET_SPECIFICATION.md.

Each store is in train, validation or test by a fixed rule on its code (the same on
every computer, so a team sharing clips agrees on it), so no store's footage is on both
sides of a comparison. Tuning looks at train and validation only ("development"); the
test set is scored only when asked, against a frozen version of the set, and every time
it is, that is recorded.

A second person can count the same footage without seeing the first count. The two are
matched the way the tool's crossings are (evaluate.agreement); the moments they disagree
on are not settled by the tool: they are left out of the scoring as uncertain, and listed.

To score a clip, an automatic count of the same store's cameras over the same period is
needed (the clean footage counted automatically in the wizard). Its recorded detections
are replayed through today's tracking and counting rule, and its crossings matched to the
clip's by clock time (both from RetailNext's file names).

With a shared folder set (e.g. the team's SharePoint library, synced by OneDrive), every
clip kept is also copied to <shared>/gold/<dataset>/, and clips found there are part of
the set on every computer.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from av.error import FFmpegError

from . import bench, paths
from . import video as vid
from .config import ConfigError
from .evaluate import MIN_SAMPLE, TOLERANCE_S, add, agreement, consensus, match, score, summary
from .gating import camera_dir
from .manual import merge_ranges
from .util import write_json_atomic
from .version import app_version
from .wizard import (
    CHOICES,
    GROUND_TRUTH_SPEC,
    MIN_WATCHED_PCT,
    default_folders,
    review_items,
    watch_stretches,
)

DATASET = "gold_v1"
SPLITS = ("train", "validation", "test")
DEVELOPMENT = ("train", "validation")
TAGS = {
    "crowd": "Heavy crowd",
    "groups": "Groups, or people crossing close together",
    "both_ways": "People going in and out at once",
    "stopping": "People stopping or turning near the line",
    "returning": "Crossing and coming straight back",
    "parallel": "People walking along the line",
    "partial": "People only partly in the picture",
    "wide_angle": "Wide-angle (fisheye) picture",
}
LIGHTING = {"normal": "Normal light", "low": "Low light", "glare": "Glare or strong sun",
            "mixed": "Changing light"}
OCCLUSION = {"none": "People hardly ever hidden", "some": "People sometimes hidden",
             "heavy": "People often hidden"}
TRAFFIC = ((40.0, "quiet"), (120.0, "normal"), (240.0, "busy"))  # crossings per camera-hour
TRAFFIC_NAMES = {"quiet": "Quiet traffic", "normal": "Normal traffic", "busy": "Busy traffic",
                 "heavy": "Heavy traffic"}
CONVENTION = "In = into the store: the side the counting line's triangles point to."
CLOCK_SLACK_S = 1.0  # an automatic count's footage must cover the clip to within this
FEW_STORES = 3  # fewer stores than this in a set: results may not carry over to others


class GoldError(Exception):
    """A clip that cannot be kept or scored, said plainly."""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read(p: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def folder(root: Path | None = None) -> Path:
    return (root or paths.data_root()) / "gold" / DATASET


def _watched_pct(ranges: list[list[float]], duration: float) -> float:
    return 100.0 * sum(b - a for a, b in merge_ranges(ranges)) / duration if duration else 0.0


def labels() -> dict[str, str]:
    """Names for every tag and condition results are broken down by."""
    return {**TAGS, **{f"traffic:{k}": v for k, v in TRAFFIC_NAMES.items()},
            **{f"lighting:{k}": v for k, v in LIGHTING.items()},
            **{f"occlusion:{k}": v for k, v in OCCLUSION.items()}}


# ---- which store is in which set -----------------------------------------------------------

def split_of(code: str) -> str:
    """A store's set, fixed by its code: the same on every computer, for good. About one
    store in five goes to test, one in five to validation, the rest to train."""
    bucket = int(hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest(), 16) % 100
    return "test" if bucket < 20 else "validation" if bucket < 40 else "train"


def traffic_level(per_camera_hour: float) -> str:
    return next((name for limit, name in TRAFFIC if per_camera_hour < limit), "heavy")


# ---- keeping a count by hand as a gold clip ------------------------------------------------

def problems(state: dict[str, Any]) -> list[str]:
    """Why this count cannot be a gold clip (nothing when it can)."""
    if state.get("mode") != "manual":
        return [("Only a count by hand can be a gold clip: a check of the tool's crossings "
                 "looks only where the tool pointed.")]
    out = []
    m, dur = state.get("manual") or {}, float(state["duration_s"])
    if state.get("marks"):
        out.append("This footage shows RetailNext's own tracks and counts, which can sway a "
                   "count towards the sensor's: a gold clip is counted on clean footage "
                   "(downloaded for a count by hand, without RetailNext's marks).")
    if not m.get("done"):
        out.append("Finish counting first.")
    elif not m.get("specification"):
        out.append("This count was finished before the Ground Truth Specification: check it "
                   f"follows v{GROUND_TRUTH_SPEC}, then press I've finished counting again.")
    for cam in state["cameras"]:
        pct = _watched_pct((m.get("watched") or {}).get(cam["sensor"], []), dur)
        if pct < MIN_WATCHED_PCT:
            out.append(f"Only {pct:.0f}% of {cam['sensor']}'s footage was watched: a gold clip "
                       f"needs every moment ({MIN_WATCHED_PCT:.0f}%).")
    if not state.get("clock_start"):
        out.append("The footage's start time is unknown (it comes from RetailNext's file name), "
                   "so the clip cannot be lined up with an automatic count.")
    if not state.get("direction"):
        out.append("Choose the traffic first.")
    if not str(state["store"].get("code") or "").strip():
        out.append("Enter the store code.")
    if not str(state["store"].get("operator") or "").strip():
        out.append("Enter your name.")
    return out


def clip_id(state: dict[str, Any]) -> str:
    start = datetime.fromisoformat(state["clock_start"])
    words = f"{state['store']['code']}-{start:%Y%m%d-%H%M%S}-{round(float(state['duration_s']) / 60)}m"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", words).strip("-")


def find(state: dict[str, Any], root: Path | None = None,
         shared: Path | None = None) -> dict[str, Any] | None:
    """The gold clip of this footage's store and period, if one was kept (here or shared)."""
    if not state.get("clock_start") or not str(state["store"].get("code") or "").strip():
        return None
    name = f"{clip_id(state)}.json"
    return _read(folder(root) / name) or (_read(folder(shared) / name) if shared else None)


def _video_facts(state: dict[str, Any]) -> dict[str, Any]:
    facts: dict[str, Any] = {"filename": state["filename"], "fingerprint": state["fingerprint"],
                             "fps": None, "width": None, "height": None}
    try:
        info = vid.probe(state["video"])
    except (OSError, ValueError, FFmpegError):
        return facts
    return {**facts, "fps": info.fps_reported, "width": info.width, "height": info.height}


def save(state: dict[str, Any], run_dir: Path, tags: Iterable[str], notes: str = "",
         root: Path | None = None, lighting: str = "normal", occlusion: str = "none",
         shared: Path | None = None) -> dict[str, Any]:
    """Keep this count by hand in the gold set: a new clip, or a second person's count of
    one already kept (the same person again replaces their own count). With a shared
    folder, the clip is also copied there."""
    bad = problems(state)
    if bad:
        raise GoldError(" ".join(bad))
    tags = sorted(set(tags))
    unknown = [t for t in tags if t not in TAGS]
    if unknown:
        raise GoldError(f"Unknown tag(s): {', '.join(unknown)}.")
    if lighting not in LIGHTING or occlusion not in OCCLUSION:
        raise GoldError("Choose the lighting and how often people are hidden.")
    dirs = list(CHOICES[state["direction"]])
    start = datetime.fromisoformat(state["clock_start"])
    reviewer = str(state["store"]["operator"]).strip()
    m, dur = state["manual"], float(state["duration_s"])
    rules = dict(state.get("rules") or {"children": "count", "staff": "count"})
    review = {
        "reviewer": reviewer, "at": _now(), "run": str(run_dir),
        "video": {"filename": state["filename"], "fingerprint": state["fingerprint"]},
        "marked": bool(state.get("marks")), "specification": m["specification"],
        "watched_pct": {c["sensor"]: round(_watched_pct(m["watched"].get(c["sensor"], []), dur), 1)
                        for c in state["cameras"]},
        "crossings": [{"camera": c["camera"], "t": round(float(c["t"]), 2),
                       "clock": (start + timedelta(seconds=float(c["t"]))).strftime("%H:%M:%S"),
                       "direction": c["direction"]}
                      for c in sorted(m["counts"], key=lambda c: float(c["t"]))
                      if c["direction"] in dirs],
    }
    cams = [{"sensor": c["sensor"], "picture": c["picture"], "config": c.get("config")}
            for c in state["cameras"]]
    path = folder(root) / f"{clip_id(state)}.json"
    rec = _read(path) or (_read(folder(shared) / path.name) if shared else None)
    if rec is None:
        code = str(state["store"]["code"]).strip()
        rec = {"schema": "gold/1", "dataset": DATASET, "id": clip_id(state),
               "store": {"code": code, "name": str(state["store"].get("name") or "")},
               "split": split_of(code),
               "period": {"start": start.isoformat(),
                          "end": (start + timedelta(seconds=dur)).isoformat(),
                          "tz": state.get("tz") or ""},
               "duration_s": dur, "video": _video_facts(state), "cameras": cams, "dirs": dirs,
               "direction_convention": CONVENTION, "specification": m["specification"],
               "rules": rules, "reviews": [], "tags": [], "notes": "", "created_at": _now()}
    else:
        if rec["dirs"] != dirs:
            raise GoldError(f"This clip was counted for {' and '.join(rec['dirs']).upper()}: "
                            f"count the same traffic.")
        if sorted(c["sensor"] for c in rec["cameras"]) != sorted(c["sensor"] for c in cams):
            raise GoldError("This clip was counted on other cameras: count the same ones.")
        if rec.get("rules", rules) != rules:
            raise GoldError("This clip was counted with other rules for children or staff: "
                            "count it with the same ones.")
    reviews = list(rec["reviews"])
    mine = [k for k, r in enumerate(reviews) if r["reviewer"].casefold() == reviewer.casefold()]
    if mine:
        reviews[mine[0]] = review
    elif len(reviews) >= 2:
        raise GoldError("Two people have counted this clip already.")
    else:
        reviews.append(review)
    first = reviews[0]
    hours = dur * len(cams) / 3600
    per_hour = round(len(first["crossings"]) / hours, 1) if hours else 0.0
    rec.update(reviews=reviews, tags=tags, notes=notes.strip(), updated_at=_now(),
               made_with=app_version(),
               conditions={"traffic": traffic_level(per_hour), "traffic_per_camera_hour": per_hour,
                           "traffic_thresholds": [limit for limit, _ in TRAFFIC],
                           "lighting": lighting, "occlusion": occlusion})
    write_json_atomic(path, rec)
    if shared is not None:
        write_json_atomic(folder(shared) / path.name, rec)
    return describe(rec)


def truth(rec: dict[str, Any]) -> tuple[dict[str, list[tuple[float, str]]], dict[str, list[float]],
                                        dict[str, Any] | None]:
    """The clip's crossings per camera, the uncertain moments per camera, and how two
    people's counts agreed (None with one count)."""
    cams, dirs = [c["sensor"] for c in rec["cameras"]], rec["dirs"]

    def of(review: dict[str, Any]) -> dict[str, list[tuple[float, str]]]:
        return {cam: [(float(c["t"]), str(c["direction"])) for c in review["crossings"]
                      if c["camera"] == cam] for cam in cams}

    first = of(rec["reviews"][0])
    if len(rec["reviews"]) < 2:
        return first, {cam: [] for cam in cams}, None
    second = of(rec["reviews"][1])
    real: dict[str, list[tuple[float, str]]] = {}
    unsure: dict[str, list[float]] = {}
    total = dict.fromkeys(("agreed", "direction_disagreements", "only_first", "only_second",
                           "disagreements"), 0)
    where: list[str] = []
    start = datetime.fromisoformat(rec["period"]["start"])
    for cam in cams:
        real[cam], unsure[cam] = consensus(first[cam], second[cam], dirs)
        g = agreement(first[cam], second[cam], dirs)
        for k in total:
            total[k] += int(g[k])
        where += [f"{cam} {(start + timedelta(seconds=t)):%H:%M:%S} ({what})"
                  for what, ts in (("direction", g["at"]["direction"]),
                                   (f"only {rec['reviews'][0]['reviewer']}", g["at"]["only_first"]),
                                   (f"only {rec['reviews'][1]['reviewer']}", g["at"]["only_second"]))
                  for t in ts]
    union = total["agreed"] + total["disagreements"]
    return real, unsure, {
        **total, "agreement_pct": round(100.0 * total["agreed"] / union, 1) if union else None,
        "status": "agreed" if not total["disagreements"] else "disagreement (unresolved)",
        "reviewers": [r["reviewer"] for r in rec["reviews"][:2]], "where": sorted(where)}


def describe(rec: dict[str, Any]) -> dict[str, Any]:
    real, unsure, agree = truth(rec)
    return {"id": rec["id"], "store": rec["store"], "split": rec["split"], "period": rec["period"],
            "duration_s": rec["duration_s"], "cameras": [c["sensor"] for c in rec["cameras"]],
            "dirs": rec["dirs"], "tags": rec["tags"], "notes": rec["notes"],
            "conditions": rec.get("conditions") or {}, "rules": rec.get("rules") or {},
            "specification": rec.get("specification"),
            "reviews": [{"reviewer": r["reviewer"], "at": r["at"], "crossings": len(r["crossings"])}
                        for r in rec["reviews"]],
            "crossings": sum(len(v) for v in real.values()),
            "uncertain": sum(len(v) for v in unsure.values()), "agreement": agree}


def _clip_files(root: Path | None = None, shared: Path | None = None
                ) -> list[tuple[Path, dict[str, Any]]]:
    """Every clip: this computer's, then any only in the shared folder (by id, ours first)."""
    out: dict[str, tuple[Path, dict[str, Any]]] = {}
    for where in [*([shared] if shared else []), root]:  # root None: this computer's data folder
        for p in sorted(folder(where).glob("*.json")):
            rec = _read(p)
            if rec is not None and rec.get("schema") == "gold/1":
                out[rec["id"]] = (p, rec)  # this computer's copy last: it wins
    return [out[k] for k in sorted(out)]


def clips(root: Path | None = None, shared: Path | None = None) -> list[dict[str, Any]]:
    return [rec for _, rec in _clip_files(root, shared)]


def splits(root: Path | None = None, shared: Path | None = None) -> dict[str, str]:
    """Each store with a gold clip, and its set."""
    return {str(r["store"]["code"]).strip().upper(): r["split"] for r in clips(root, shared)}


# ---- versions: frozen, checksummed manifests -----------------------------------------------

def manifests_dir(root: Path | None = None) -> Path:
    return folder(root) / "manifests"


def _sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def manifest(root: Path | None = None, shared: Path | None = None,
             records: bool = False) -> dict[str, Any]:
    """What the set holds right now, with a hash of its contents."""
    files = _clip_files(root, shared)
    entries = []
    for p, rec in files:
        entry = {"id": rec["id"], "split": rec["split"], "sha256": _sha_file(p),
                 "crossings": describe(rec)["crossings"]}
        if records:  # a frozen version keeps the clips themselves, so it can be scored later
            entry["record"] = rec
        entries.append(entry)
    content = hashlib.sha256(json.dumps([[e["id"], e["sha256"]] for e in entries]).encode()).hexdigest()
    return {"dataset_id": DATASET, "content_hash": content,
            "ground_truth_versions": sorted({str(r.get("specification")) for _, r in files
                                             if r.get("specification")}),
            "stores": sorted({str(r["store"]["code"]) for _, r in files}),
            "cameras": sorted({f"{r['store']['code']}/{c['sensor']}" for _, r in files
                               for c in r["cameras"]}),
            "videos": sorted({str(v["video"]["fingerprint"]) for _, r in files for v in r["reviews"]}),
            "crossings": sum(e["crossings"] for e in entries),
            "splits": {str(r["store"]["code"]): r["split"] for _, r in files},
            "clips": entries}


def releases(root: Path | None = None) -> list[dict[str, Any]]:
    """Frozen versions, oldest first."""
    found = [r for p in manifests_dir(root).glob(f"{DATASET}.*.json") if (r := _read(p))]
    return sorted(found, key=lambda r: int(str(r["dataset_version"]).rsplit(".", 1)[1]))


def version_of(current: dict[str, Any], root: Path | None = None) -> str:
    """The frozen version the set matches exactly, or "unreleased"."""
    same = [r for r in releases(root) if r.get("content_hash") == current["content_hash"]]
    return str(same[-1]["dataset_version"]) if same else "unreleased"


def freeze(root: Path | None = None, note: str = "", shared: Path | None = None) -> dict[str, Any]:
    """Freeze the set as it is: a new version, written once and never rewritten."""
    now = manifest(root, shared, records=True)
    if not now["clips"]:
        raise GoldError("Nothing to freeze: there is no gold clip yet.")
    known = releases(root)
    same = next((r for r in known if r["content_hash"] == now["content_hash"]), None)
    if same is not None:
        return same
    n = max((int(str(r["dataset_version"]).rsplit(".", 1)[1]) for r in known), default=0) + 1
    rel = {"dataset_version": f"{DATASET}.{n}", "created_at": _now(), "note": note.strip(),
           "made_with": app_version(), **now}
    path = manifests_dir(root) / f"{DATASET}.{n}.json"
    if path.exists():
        raise GoldError(f"{path.name} exists already: a frozen version is never rewritten.")
    write_json_atomic(path, rel)
    return rel


def check(root: Path | None = None, shared: Path | None = None) -> list[str]:
    """What is wrong with the set's files, or has changed since a version was frozen."""
    out = []
    for where in [root, *([shared] if shared else [])]:  # root None: this computer's data folder
        for p in sorted(folder(where).glob("*.json")):
            rec = _read(p)
            if rec is None or rec.get("schema") != "gold/1":
                out.append(f"{p.name}: not a readable gold clip")
                continue
            code = str(rec["store"]["code"])
            if rec["split"] != split_of(code):
                out.append(f"{rec['id']}: marked {rec['split']}, but store {code} belongs to "
                           f"{split_of(code)}")
    current = {c["id"]: c["sha256"] for c in manifest(root, shared)["clips"]}
    for rel in releases(root):
        changed = [c["id"] for c in rel["clips"] if current.get(c["id"]) != c["sha256"]]
        if changed:
            out.append(f"{rel['dataset_version']}: {len(changed)} clip(s) changed or gone since it "
                       f"was frozen ({', '.join(changed[:3])}); the frozen version keeps them as "
                       f"they were.")
    return out


# ---- scoring the automatic count on gold clips ---------------------------------------------

def find_run(rec: dict[str, Any], runs_root: Path) -> tuple[Path, float, set[str]] | None:
    """An automatic count (with recorded detections) of the clip's cameras over its whole
    period: its run folder, the clip's start in that footage's seconds, the cameras."""
    start = datetime.fromisoformat(rec["period"]["start"])
    end = datetime.fromisoformat(rec["period"]["end"])
    sensors = {c["sensor"] for c in rec["cameras"]}
    found: dict[Path, tuple[float, set[str]]] = {}
    for act_path in sorted(runs_root.glob("*/*/activity.json")):
        if not (act_path.parent / "detections.pkl").is_file():
            continue
        act = _read(act_path) or {}
        sensor = str((act.get("config") or {}).get("sensor", ""))
        iv = (act.get("timebase") or {}).get("filename_interval")
        if sensor not in sensors or not iv:
            continue
        rs, re_ = datetime.fromisoformat(iv["start"]), datetime.fromisoformat(iv["end"])
        if (start - rs).total_seconds() < -CLOCK_SLACK_S or (re_ - end).total_seconds() < -CLOCK_SLACK_S:
            continue
        offset, have = found.get(act_path.parent.parent, ((start - rs).total_seconds(), set()))
        have.add(sensor)
        found[act_path.parent.parent] = (offset, have)
    if not found:
        return None
    run_dir, (offset, have) = max(found.items(), key=lambda kv: (len(kv[1][1]), kv[0].name))
    return run_dir, offset, have


def _video_of(run_dir: Path) -> Path:
    st = _read(run_dir / "wizard" / "state.json") or {}
    options: list[str] = [str(st.get("video") or "")]
    for p in sorted(run_dir.glob("*/activity.json")):
        v = (_read(p) or {}).get("video") or {}
        options.append(str(v.get("path") or ""))
        if v.get("filename"):
            options += [str(f / str(v["filename"])) for f in default_folders()]
    for o in options:
        if o and Path(o).is_file():
            return Path(o)
    raise GoldError("the video of its automatic count is no longer on this computer")


@functools.lru_cache(maxsize=16)
def _sha(path: str, mtime: float) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _detectors(run_dir: Path, sensors: Iterable[str]) -> list[dict[str, Any]]:
    """The detector each camera's automatic count used, with its weights file's SHA-256."""
    out: dict[str, dict[str, Any]] = {}
    for sensor in sensors:
        det = (_read(camera_dir(run_dir, sensor) / "candidates.json") or {}).get("detector") or {}
        name = str(det.get("model") or "")
        if not name or name in out:
            continue
        weights = next((d / name for d in paths.models_dirs() if (d / name).is_file()), None)
        out[name] = {"model": name, "mode": det.get("mode"), "conf": det.get("conf"),
                     "sha256": _sha(str(weights), weights.stat().st_mtime) if weights else None}
    return list(out.values())


def score_camera(real: list[tuple[float, str]], unsure: list[float], items: list[dict[str, Any]],
                 stretches: list[dict[str, Any]], offset: float, duration: float,
                 dirs: list[str]) -> dict[str, Any]:
    """One camera of a clip: the tool's crossings (moved onto the clip's clock and kept to
    its period) against the person's, and the reviewing it would have asked for."""
    def inside(t: float) -> bool:
        return 0.0 <= t <= duration

    counted = [(float(i["t"]) - offset, str(i["direction"])) for i in items
               if i["kind"] == "counted" and inside(float(i["t"]) - offset)]
    listed = [(float(i["t"]) - offset, str(i["direction"])) for i in items
              if i["kind"] == "possible" and inside(float(i["t"]) - offset)]
    s = score(real, counted, dirs, unsure)
    missed = s["at"]["missed"]
    on_list = len(match(missed, [t for t, _ in listed]))
    watch_s = sum(max(0.0, min(w["end"] - offset, duration) - max(w["start"] - offset, 0.0))
                  for w in stretches)
    return {"by_direction": s["by_direction"], "at": s["at"], "questions": len(counted) + len(listed),
            "listed": len(listed), "missed": len(missed), "misses_on_list": on_list,
            "watch_s": round(watch_s, 1)}


def score_clip(rec: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    cond = rec.get("conditions") or {}
    keys = [*rec["tags"], *(f"{k}:{cond[k]}" for k in ("traffic", "lighting", "occlusion")
                            if cond.get(k))]
    base = {"id": rec["id"], "split": rec["split"], "tags": rec["tags"], "groups": keys,
            "store": rec["store"]["code"], "scored": False}
    real, unsure, agree = truth(rec)
    base.update(agreement=agree, uncertain=sum(len(v) for v in unsure.values()))
    hit = find_run(rec, (root or paths.data_root()) / "runs")
    if hit is None:
        return {**base, "reason": (
            "No automatic count of these cameras over this period on this computer yet: "
            "download the same period (Footage > RetailNext > Automatic) and count it "
            "automatically.")}
    run_dir, offset, have = hit
    try:
        video = _video_of(run_dir)
        results, notes = bench.replay(video, run_dir, list(bench._configs(run_dir, have)))
    except (GoldError, bench.BenchError, ConfigError, OSError, ValueError) as e:
        return {**base, "run": run_dir.name, "reason": f"{run_dir.name}: {e}"}
    dur, dirs = float(rec["duration_s"]), list(rec["dirs"])
    cams: dict[str, Any] = {}
    by_dir: dict[str, dict[str, int]] = {}
    for r in results:
        sensor = r.cfg.sensor
        if sensor not in have:
            continue
        pic = int(r.candidates.get("picture", {}).get("index", 0))
        items = review_items(sensor, pic, r.candidates, r.discarded, r.unexplained, dirs,
                             float("inf"))
        cams[sensor] = score_camera(real.get(sensor, []), unsure.get(sensor, []), items,
                                    watch_stretches(sensor, pic, r.unexplained), offset, dur, dirs)
        add(by_dir, cams[sensor]["by_direction"])
    notes += [f"{c}: no automatic count of this camera, not scored"
              for c in real if c not in cams]
    work = {k: sum(c[k] for c in cams.values())
            for k in ("questions", "listed", "missed", "misses_on_list", "watch_s")}
    return {**base, "scored": True, "run": run_dir.name, "offset_s": round(offset, 2),
            "cameras": cams, "by_direction": by_dir, "camera_hours": dur * len(cams) / 3600,
            **work, "detectors": _detectors(run_dir, have), "notes": notes}


def _workload(scored: list[dict[str, Any]]) -> dict[str, Any]:
    """The reviewing the tool asks of a person, per hour of one camera's footage."""
    hours = sum(c["camera_hours"] for c in scored)
    q, listed = sum(c["questions"] for c in scored), sum(c["listed"] for c in scored)
    missed, on_list = sum(c["missed"] for c in scored), sum(c["misses_on_list"] for c in scored)
    watch = sum(c["watch_s"] for c in scored)

    def per_hour(x: float) -> float | None:
        return round(x / hours, 1) if hours else None

    return {"camera_hours": round(hours, 2), "questions_per_hour": per_hour(q),
            "possible_misses_per_hour": per_hour(listed),
            "watch_minutes_per_hour": per_hour(watch / 60),
            "misses": missed, "misses_on_list": on_list,
            "misses_on_list_pct": round(100.0 * on_list / missed, 1) if missed else None,
            "real_misses_per_100_listed": round(100.0 * on_list / listed, 1) if listed else None}


def _limits(recs: list[dict[str, Any]], scored: list[dict[str, Any]], total: int) -> list[str]:
    out = []
    if total < MIN_SAMPLE:
        out.append(f"Insufficient sample: {total} crossing(s) scored, at least {MIN_SAMPLE} "
                   f"needed for any rate.")
    stores = {r["store"]["code"] for r in recs}
    if len(stores) < FEW_STORES:
        out.append(f"Only {len(stores)} store(s): results may not carry over to other stores.")
    unscored = len(recs) - len(scored)
    if unscored:
        out.append(f"{unscored} clip(s) not scored: see each clip's reason.")
    unsure = sum(c["uncertain"] for c in scored)
    if unsure:
        out.append(f"{unsure} moment(s) left out as uncertain: the two people who counted "
                   f"disagreed there.")
    if not any(len(r["reviews"]) > 1 for r in recs):
        out.append("No clip has been counted by a second person: how far two people agree is "
                   "not known yet.")
    return out


def experiments_dir(root: Path | None = None) -> Path:
    return (root or paths.data_root()) / "bench" / "experiments"


def evaluate(which: str, root: Path | None = None, note: str = "",
             shared: Path | None = None) -> dict[str, Any]:
    """Score the automatic count on the development clips (train and validation) or, when
    asked, on the held-out test clips, which needs a frozen version of the set. The result
    is kept as an experiment record naming the dataset version it used."""
    if which not in ("development", "test"):
        raise GoldError("Score the development set or the test set.")
    chosen = DEVELOPMENT if which == "development" else ("test",)
    current = manifest(root, shared)
    version = version_of(current, root)
    if which == "test" and version == "unreleased":
        raise GoldError("Freeze the gold set first (Versions, on the gold page): a test-set score "
                        "must name a frozen version of the set.")
    recs = [r for r in clips(root, shared) if r["split"] in chosen]
    if not recs:
        raise GoldError(f"No gold clip in the {which} set yet.")
    results = [score_clip(r, root) for r in recs]
    scored = [c for c in results if c["scored"]]
    total: dict[str, dict[str, int]] = {}
    by_split: dict[str, dict[str, dict[str, int]]] = {}
    by_group: dict[str, dict[str, dict[str, int]]] = {}
    for c in scored:
        add(total, c["by_direction"])
        add(by_split.setdefault(c["split"], {}), c["by_direction"])
        for g in c["groups"]:
            add(by_group.setdefault(g, {}), c["by_direction"])
    n = sum(v["truth"] for v in total.values())
    made = datetime.now().astimezone()
    ids = {r["id"] for r in recs}
    exp = {"id": f"{made:%Y%m%d-%H%M%S}-{which}", "made_at": made.isoformat(timespec="seconds"),
           "set": which, "splits": list(chosen),
           "dataset": {"id": DATASET, "version": version, "content_hash": current["content_hash"],
                       "clips": [{k: c[k] for k in ("id", "split", "sha256")}
                                 for c in current["clips"] if c["id"] in ids]},
           "app_version": app_version(),
           "settings": {**bench.settings(), "tolerance_s": TOLERANCE_S, "min_sample": MIN_SAMPLE,
                        "ground_truth_specification": GROUND_TRUTH_SPEC},
           "detectors": sorted({d["model"]: d for c in scored for d in c["detectors"]}.values(),
                               key=lambda d: str(d["model"])),
           "clips": results, "totals": summary(total) if total else None,
           "by_split": {k: summary(v) for k, v in by_split.items()},
           "by_tag": {k: summary(v) for k, v in sorted(by_group.items())},
           "workload": _workload(scored), "limits": _limits(recs, scored, n), "note": note}
    write_json_atomic(experiments_dir(root) / f"{exp['id']}.json", exp)
    return exp


def experiments(root: Path | None = None) -> list[dict[str, Any]]:
    """Earlier scorings, newest first, in brief."""
    out = []
    for p in sorted(experiments_dir(root).glob("*.json"), reverse=True):
        e = _read(p)
        if not e:
            continue
        tot = (e.get("totals") or {}).get("all") or {}
        ds = e.get("dataset")
        out.append({"id": e["id"], "made_at": e["made_at"], "set": e["set"],
                    "app_version": e.get("app_version"),
                    "dataset_version": ds.get("version") if isinstance(ds, dict) else ds,
                    "clips": sum(1 for c in e.get("clips", []) if c.get("scored")),
                    "crossings": tot.get("truth", 0), "recall": tot.get("recall"),
                    "precision": tot.get("precision"), "sufficient": tot.get("sufficient", False)})
    return out


def overview(root: Path | None = None, shared: Path | None = None) -> dict[str, Any]:
    recs = clips(root, shared)
    sets: dict[str, dict[str, Any]] = {s: {"clips": 0, "stores": set(), "crossings": 0}
                                       for s in SPLITS}
    described = [describe(r) for r in recs]
    for d in described:
        s = sets[d["split"]]
        s["clips"] += 1
        s["stores"].add(d["store"]["code"])
        s["crossings"] += d["crossings"]
    exps = experiments(root)
    current = manifest(root, shared)
    return {"dataset": DATASET, "tags": TAGS, "labels": labels(), "lighting": LIGHTING,
            "occlusion": OCCLUSION, "clips": described,
            "sets": {k: {**v, "stores": sorted(v["stores"])} for k, v in sets.items()},
            "experiments": exps, "test_scorings": sum(1 for e in exps if e["set"] == "test"),
            "version": version_of(current, root), "content_hash": current["content_hash"],
            "releases": [{k: r.get(k) for k in ("dataset_version", "created_at", "note",
                                                 "crossings", "content_hash")}
                         | {"clips": len(r.get("clips", []))} for r in releases(root)],
            "checks": check(root, shared), "shared": str(folder(shared)) if shared else None,
            "tolerance_s": TOLERANCE_S, "min_sample": MIN_SAMPLE,
            "specification": GROUND_TRUTH_SPEC}
