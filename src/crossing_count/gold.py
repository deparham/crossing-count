"""The gold set: clips a person counted in full, kept to measure the automatic count.

A gold clip is a count by hand in the wizard (Manual) that watched at least MIN_WATCHED_PCT
of every counted camera's footage: every crossing in it was looked for, not only those the
tool pointed at. ("Every crossing the tool proposed was checked" is a different thing and
never makes a gold clip.) Each clip is one JSON file under gold/<dataset>/, written only
from people's counts, never by a model. See docs/DATASET_SPECIFICATION.md.

A clip is one of two tiers, by its footage (independence.py: what the picture shows first):
  clean   nothing of the sensor's on the picture. The answer key: every score, the test set.
  marked  RetailNext's tracks and counts on the picture, which can sway a count towards the
          sensor's. Scored in the development set only, and always shown apart from clean
          clips; never in the test set, never in anything said about the sensor's accuracy.
A window counted on both keeps two clips (the marked one's id ends in -marked). Such pairs
measure how far the marks move a count (marks_effect()), instead of guessing it.

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
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from av.error import FFmpegError

from . import auditlog, bench, independence, paths
from . import video as vid
from .config import ConfigError
from .evaluate import ENGINE as CROSSING_ENGINE
from .evaluate import (
    MATCHING,
    MIN_SAMPLE,
    TOLERANCE_S,
    add,
    agreement,
    consensus,
    lag,
    match,
    score,
    summary,
)
from .gating import camera_dir
from .manual import intervals_for, merge_ranges
from .util import write_json_atomic
from .validation import TRAFFIC, TRAFFIC_NAMES, traffic_level
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
CONVENTION = "In = into the store: the side the counting line's triangles point to."
CLOCK_SLACK_S = 1.0  # an automatic count's footage must cover the clip to within this
FEW_STORES = 3  # fewer stores than this in a set: results may not carry over to others
TIERS = ("clean", "marked")
MARKED_SUFFIX = "-marked"  # a marked clip's id: the clean clip of the same window keeps the plain id
PAIRS_FOR_A_VERDICT = 5  # windows counted both ways before anything is said about the marks' effect


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


# ---- keeping a count by hand as a gold clip ------------------------------------------------

def problems(state: dict[str, Any]) -> list[str]:
    """Why this count cannot be a gold clip (nothing when it can)."""
    if state.get("mode") != "manual":
        return [("Only a count by hand can be a gold clip: a check of the tool's crossings "
                 "looks only where the tool pointed.")]
    out = []
    m, dur = state.get("manual") or {}, float(state["duration_s"])
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


def footage_tier(state: dict[str, Any]) -> str:
    """clean or marked: what this count's footage is (the picture, the download, the name,
    the answer; independence.py)."""
    return "clean" if independence.of_state(state)["clean"] else "marked"


TIER_WORDS = {
    ("clean", "dev"): "Clean footage: scored in the development set.",
    ("clean", "test"): "Clean footage: kept for the final check on the test set.",
    ("marked", "dev"): ("Counted on footage showing RetailNext's marks: scored in the development "
                        "set, shown apart from clean clips; never in the test set or in anything "
                        "said about RetailNext's accuracy."),
    ("marked", "test"): ("Counted on footage showing RetailNext's marks, in a test-set store: the "
                         "test set is clean footage only, so it is not scored. Counted on clean "
                         "footage too, it shows how far the marks sway a count."),
}


def tier_words(tier_: str, split: str) -> str:
    return TIER_WORDS[(tier_, "test" if split == "test" else "dev")]


def clip_id(state: dict[str, Any]) -> str:
    """The window's id: store, start and length; a count on marked footage ends in -marked."""
    start = datetime.fromisoformat(state["clock_start"])
    words = f"{state['store']['code']}-{start:%Y%m%d-%H%M%S}-{round(float(state['duration_s']) / 60)}m"
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", words).strip("-")
    return base + (MARKED_SUFFIX if footage_tier(state) == "marked" else "")


def window_id(clip: str) -> str:
    """The window a clip id names, whichever footage it was counted on."""
    return clip.removesuffix(MARKED_SUFFIX)


def _system_counts(state: dict[str, Any], dirs: list[str]) -> dict[str, Any] | None:
    """The system's own numbers for exactly this window, kept with the count: whole 15-minute
    intervals only (a part interval's number covers people outside the clip)."""
    sensor = state.get("sensor") or {}
    if any(sensor.get(d) is None for d in dirs) or not state.get("clock_start"):
        return None
    ivs = intervals_for(datetime.fromisoformat(state["clock_start"]), float(state["duration_s"]))
    if not ivs or not all(i["full"] for i in ivs):
        return None
    return {"system": str((state.get("sensor_source") or {}).get("system") or "RetailNext"),
            "counts": {d: int(sensor[d]) for d in dirs}}


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
    folder, the clip is also copied there. The footage is checked in the picture for
    RetailNext's marks, whatever the wizard was told about it."""
    if state.get("marks_detected") is None:
        try:
            state = {**state, "marks_detected": independence.detect(Path(str(state["video"])))}
        except (OSError, ValueError, FFmpegError) as e:
            raise GoldError(f"The footage could not be checked for RetailNext's marks ({e}): a "
                            f"gold clip needs it.") from None
    bad = problems(state)
    if bad:
        raise GoldError(" ".join(bad))
    footage = independence.of_state(state)
    tier_ = "clean" if footage["clean"] else "marked"
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
        "marked": tier_ == "marked", "footage": footage,
        "specification": m["specification"],
        "watched_pct": {c["sensor"]: round(_watched_pct(m["watched"].get(c["sensor"], []), dur), 1)
                        for c in state["cameras"]},
        "crossings": [{"camera": c["camera"], "t": round(float(c["t"]), 2),
                       "clock": (start + timedelta(seconds=float(c["t"]))).strftime("%H:%M:%S"),
                       "direction": c["direction"],
                       **({"uncertain": True} if c.get("uncertain") else {}),
                       **({"note": c["note"]} if c.get("note") else {})}
                      for c in sorted(m["counts"], key=lambda c: float(c["t"]))
                      if c["direction"] in dirs],
        # to see whether a count on marked footage moved towards the sensor's (marks_effect)
        "system_counts": _system_counts(state, dirs),
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
               "tier": tier_, "rules": rules, "reviews": [], "tags": [], "notes": "",
               "created_at": _now()}
    else:
        if tier(rec) != tier_:  # kept before tiers, under the plain id
            raise GoldError(f"This window's gold clip {rec['id']} was counted on "
                            f"{tier(rec)} footage before clean and marked counts were kept apart, "
                            f"and this count is on {tier_} footage: they cannot share a clip.")
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
    people's counts agreed (None with one count).

    Crossings marked uncertain are never truth: they are uncertain moments. With two counts,
    the crossings both counted the same way are truth; each moment they disagree on (and
    each uncertain mark) is a dispute, uncertain until someone settles it (adjudicate()):
    settled as In or Out it becomes a crossing, as no crossing it goes, as uncertain it
    stays out of scoring. Both original counts are always kept."""
    cams, dirs = [c["sensor"] for c in rec["cameras"]], rec["dirs"]
    reviews = rec["reviews"]

    def of(review: dict[str, Any], marked: bool = False) -> dict[str, list[tuple[float, str]]]:
        return {cam: [(float(c["t"]), str(c["direction"])) for c in review["crossings"]
                      if c["camera"] == cam and bool(c.get("uncertain")) == marked] for cam in cams}

    first = of(reviews[0])
    if len(reviews) < 2:
        return first, {cam: [t for t, _ in of(reviews[0], True)[cam]] for cam in cams}, None
    second = of(reviews[1])
    names = [str(r["reviewer"]) for r in reviews[:2]]
    marks = [of(reviews[0], True), of(reviews[1], True)]
    decided: dict[str, dict[str, Any]] = (rec.get("adjudication") or {}).get("decisions") or {}
    start = datetime.fromisoformat(rec["period"]["start"])
    real: dict[str, list[tuple[float, str]]] = {}
    unsure: dict[str, list[float]] = {}
    total = dict.fromkeys(("agreed", "direction_disagreements", "only_first", "only_second",
                           "disagreements"), 0)
    disputes: list[dict[str, Any]] = []

    def way(src: list[tuple[float, str]], t: float) -> str:
        return next((d.upper() for tt, d in src if abs(tt - t) < 0.01), "?")

    for cam in cams:
        agreed, _ = consensus(first[cam], second[cam], dirs)
        g = agreement(first[cam], second[cam], dirs)
        for k in total:
            total[k] += int(g[k])
        mine = [(t, f"{names[0]} counted {way(first[cam], t)}, {names[1]} the other way")
                for t in g["at"]["direction"]]
        mine += [(t, f"only {names[0]} counted {way(first[cam], t)}") for t in g["at"]["only_first"]]
        mine += [(t, f"only {names[1]} counted {way(second[cam], t)}")
                 for t in g["at"]["only_second"]]
        for who, m in zip(names, marks, strict=True):
            for t, d in m[cam]:
                near = next((k for k, (tt, _) in enumerate(mine) if abs(tt - t) <= TOLERANCE_S), None)
                if near is None:
                    mine.append((t, f"{who} marked an uncertain {d.upper()}"))
                else:  # the same moment as another disagreement: one question, not two
                    mine[near] = (mine[near][0], f"{mine[near][1]}; {who} marked it uncertain")
        real[cam], unsure[cam] = list(agreed), []
        for t, what in sorted(mine):
            key = f"{cam}@{t:.2f}"
            decision = decided.get(key)
            disputes.append({"key": key, "camera": cam, "t": round(t, 2),
                             "clock": f"{(start + timedelta(seconds=t)):%H:%M:%S}", "what": what,
                             "decision": decision})
            if decision and decision["decision"] in dirs:
                real[cam].append((t, str(decision["decision"])))
            elif not decision or decision["decision"] == "uncertain":
                unsure[cam].append(t)
        real[cam].sort()
    union = total["agreed"] + total["disagreements"]
    settled = sum(1 for d in disputes if d["decision"])
    status = ("agreed" if not disputes else "settled" if settled == len(disputes)
              else "partly settled" if settled else "disagreement (unresolved)")
    return real, unsure, {
        **total, "agreement_pct": round(100.0 * total["agreed"] / union, 1) if union else None,
        "status": status, "reviewers": names, "disputes": disputes,
        "where": [f"{d['camera']} {d['clock']} ({d['what']})" for d in disputes]}


def _review_marked(r: dict[str, Any]) -> bool:
    f = r.get("footage")
    return bool(r.get("marked") or (f and not f.get("clean"))
                or independence.name_says_marked(str((r.get("video") or {}).get("filename") or "")))


def tier(rec: dict[str, Any]) -> str:
    """clean or marked. Marked wins: a clip is marked when it says so or any of its counts
    was on marked footage (clips kept before tiers say nothing)."""
    marked = rec.get("tier") == "marked" or any(_review_marked(r) for r in rec["reviews"])
    return "marked" if marked else "clean"


def marked_why(rec: dict[str, Any]) -> list[str]:
    """What showed each count's footage to be marked."""
    out = []
    for r in rec["reviews"]:
        if _review_marked(r):
            detail = "; ".join((r.get("footage") or {}).get("why_marked") or [])
            out.append(f"{r.get('reviewer') or 'someone'} counted it on footage showing "
                       f"RetailNext's marks" + (f" ({detail})" if detail else ""))
    return out


def provisional(rec: dict[str, Any]) -> list[str]:
    """Why a clean clip is not trusted as clean: its footage was never checked for
    RetailNext's marks in the picture (kept before that check). Kept and listed, left out of
    scoring unless asked. A marked clip is not provisional: it is the marked tier."""
    if tier(rec) == "marked":
        return []
    return [f"{r.get('reviewer') or 'someone'}'s footage was not checked for RetailNext's marks in "
            f"the picture (kept before that check): save the count again to check it"
            for r in rec["reviews"]
            if not (r.get("footage") or {}).get("checked_in_picture")]


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
            "uncertain": sum(len(v) for v in unsure.values()), "agreement": agree,
            "tier": tier(rec), "window": window_id(rec["id"]), "marked_why": marked_why(rec),
            "scoring": tier_words(tier(rec), rec["split"]), "provisional": provisional(rec)}


DECISIONS = ("in", "out", "none", "uncertain")


def adjudicate(state: dict[str, Any], decisions: Iterable[dict[str, Any]], by: str,
               root: Path | None = None, shared: Path | None = None) -> dict[str, Any]:
    """Settle moments two people's counts disagree on: for each, In, Out, no crossing or
    still uncertain, who decided and why. Both counts stay as they were; a later decision on
    the same moment replaces the earlier one, and every decision stays in the history and the
    audit log."""
    by = by.strip()
    if not by:
        raise GoldError("Say who settles the disagreements.")
    rec = find(state, root, shared)
    if rec is None:
        raise GoldError("This footage has no gold clip.")
    if len(rec["reviews"]) < 2:
        raise GoldError("Only one person has counted this clip: there is nothing to settle.")
    _, _, agree = truth(rec)
    assert agree is not None
    open_ = {d["key"]: d for d in agree["disputes"]}
    names = {r["reviewer"].casefold() for r in rec["reviews"][:2]}
    adj = rec.setdefault("adjudication", {"decisions": {}, "history": []})
    for d in decisions:
        key = f"{d.get('camera')}@{float(d.get('t', -1)):.2f}"
        if key not in open_:
            raise GoldError(f"There is no disagreement on {d.get('camera')} at {d.get('t')}.")
        decision, reason = str(d.get("decision") or ""), str(d.get("reason") or "").strip()
        if decision not in DECISIONS or (decision in ("in", "out") and decision not in rec["dirs"]):
            raise GoldError(f"{decision!r}: settle it as {', '.join(rec['dirs'])}, none or uncertain.")
        if not reason:
            raise GoldError("Give the reason for each decision: it is kept with it.")
        entry = {"decision": decision, "reason": reason, "by": by, "at": _now(),
                 "by_one_of_the_counters": by.casefold() in names}
        adj["decisions"][key] = entry
        adj["history"].append({"key": key, "dispute": open_[key]["what"], **entry})
        auditlog.append("adjudicated", user=by, obj={"gold_clip": rec["id"], "camera": d["camera"],
                                                     "t": open_[key]["t"]},
                        before=open_[key]["what"], after=decision, reason=reason, root=root)
    rec["updated_at"] = _now()
    write_json_atomic(folder(root) / f"{rec['id']}.json", rec)
    if shared is not None:
        write_json_atomic(folder(shared) / f"{rec['id']}.json", rec)
    return describe(rec)


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
        entry = {"id": rec["id"], "split": rec["split"], "tier": tier(rec), "sha256": _sha_file(p),
                 "crossings": describe(rec)["crossings"]}
        if why := provisional(rec):  # listed, never silently dropped
            entry["provisional"] = why
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
            "provisional": [e["id"] for e in entries if e.get("provisional")],
            "tiers": {k: sum(1 for e in entries if e["tier"] == k) for k in TIERS},
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
                     "backbone": det.get("backbone") or "yolo",
                     "sha256": det.get("weights_sha256")
                     or (_sha(str(weights), weights.stat().st_mtime) if weights else None)}
    return list(out.values())


def detector_of(c: Mapping[str, Any]) -> set[tuple[str, str, str]]:
    """Which detector (and which weights) a clip's automatic count used."""
    return {(str(d.get("backbone") or "yolo"), str(d.get("model") or "?"), str(d.get("sha256")))
            for d in c.get("detectors") or []}


def score_camera(real: list[tuple[float, str]], unsure: list[float], items: list[dict[str, Any]],
                 stretches: list[dict[str, Any]], offset: float, duration: float,
                 dirs: list[str], lag: float = 0.0) -> dict[str, Any]:
    """One camera of a clip: the tool's crossings (moved onto the clip's clock and kept to
    its period) against the person's, and the reviewing it would have asked for.

    lag moves the tool's crossings onto the counter's clock (evaluate.lag): a count made by
    hand is pressed after the crossing is seen, which is the counter's reaction, not the
    tool's error."""
    def inside(t: float) -> bool:
        return 0.0 <= t <= duration

    counted = [(float(i["t"]) - offset + lag, str(i["direction"])) for i in items
               if i["kind"] == "counted" and inside(float(i["t"]) - offset)]
    listed = [(float(i["t"]) - offset + lag, str(i["direction"])) for i in items
              if i["kind"] == "possible" and inside(float(i["t"]) - offset)]
    s = score(real, counted, dirs, unsure)
    missed = s["at"]["missed"]
    on_list = len(match(missed, [t for t, _ in listed]))
    watch_s = sum(max(0.0, min(w["end"] - offset, duration) - max(w["start"] - offset, 0.0))
                  for w in stretches)
    return {"by_direction": s["by_direction"], "at": s["at"], "questions": len(counted) + len(listed),
            "listed": len(listed), "missed": len(missed), "misses_on_list": on_list,
            "watch_s": round(watch_s, 1)}


def score_clip(rec: dict[str, Any], root: Path | None = None,
               tracking: bench.Tracking | None = None) -> dict[str, Any]:
    cond = rec.get("conditions") or {}
    keys = [*rec["tags"], *(f"{k}:{cond[k]}" for k in ("traffic", "lighting", "occlusion")
                            if cond.get(k))]
    base = {"id": rec["id"], "split": rec["split"], "tags": rec["tags"], "groups": keys,
            "store": rec["store"]["code"], "tier": tier(rec), "window": window_id(rec["id"]),
            "scored": False}
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
        results, notes = bench.replay(video, run_dir, list(bench._configs(run_dir, have)),
                                      tracking=tracking)
    except (GoldError, bench.BenchError, ConfigError, OSError, ValueError) as e:
        return {**base, "run": run_dir.name, "reason": f"{run_dir.name}: {e}"}
    dur, dirs = float(rec["duration_s"]), list(rec["dirs"])
    cams: dict[str, Any] = {}
    by_dir: dict[str, dict[str, int]] = {}
    found_items: dict[str, tuple[Any, list[dict[str, Any]], int]] = {}
    for r in results:
        sensor = r.cfg.sensor
        if sensor not in have:
            continue
        pic = int(r.candidates.get("picture", {}).get("index", 0))
        found_items[sensor] = (r, review_items(sensor, pic, r.candidates, r.discarded,
                                               r.unexplained, dirs, float("inf")), pic)
    # the two clocks, over the whole clip: a hand count is pressed after the crossing is seen
    every_real = [x for sensor in found_items for x in real.get(sensor, [])]
    every_tool = [(float(i["t"]) - offset, str(i["direction"]))
                  for _, items, _ in found_items.values() for i in items if i["kind"] == "counted"]
    hand_lag = lag(every_real, every_tool, dirs) if rec.get("reviews") else 0.0
    for sensor, (r, items, pic) in found_items.items():
        cams[sensor] = score_camera(real.get(sensor, []), unsure.get(sensor, []), items,
                                    watch_stretches(sensor, pic, r.unexplained), offset, dur,
                                    dirs, hand_lag)
        add(by_dir, cams[sensor]["by_direction"])
    notes += [f"{c}: no automatic count of this camera, not scored"
              for c in real if c not in cams]
    work = {k: sum(c[k] for c in cams.values())
            for k in ("questions", "listed", "missed", "misses_on_list", "watch_s")}
    if hand_lag:
        notes.append(f"The hand count was pressed a median {abs(hand_lag):.1f}s "
                     f"{'after' if hand_lag > 0 else 'before'} the tool's moment (a counter's "
                     f"reaction time): the two clocks were aligned before matching.")
    return {**base, "scored": True, "run": run_dir.name, "offset_s": round(offset, 2),
            "hand_lag_s": hand_lag, "cameras": cams, "by_direction": by_dir,
            "camera_hours": dur * len(cams) / 3600,
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
             shared: Path | None = None, include_provisional: bool = False,
             clean_only: bool = False,
             tracking: bench.Tracking | None = None) -> dict[str, Any]:
    """Score the automatic count on the development clips (train and validation) or, when
    asked, on the held-out test clips, which needs a frozen version of the set. The result
    is kept as an experiment record naming the dataset version it used.

    Left out, and named: provisional clips (provisional()) unless asked for; marked clips from
    the test set always, and from the development set when clean_only. Clean and marked clips
    are scored apart (by_tier); the totals count a window counted both ways once, from its
    clean count."""
    if which not in ("development", "test"):
        raise GoldError("Score the development set or the test set.")
    chosen = DEVELOPMENT if which == "development" else ("test",)
    clean_only = clean_only or which == "test"
    current = manifest(root, shared)
    version = version_of(current, root)
    if which == "test" and version == "unreleased":
        raise GoldError("Freeze the gold set first (Versions, on the gold page): a test-set score "
                        "must name a frozen version of the set.")
    in_set = [r for r in clips(root, shared) if r["split"] in chosen]
    left_out: list[dict[str, Any]] = []
    for r in in_set:
        if clean_only and tier(r) == "marked":
            left_out.append({"id": r["id"], "tier": "marked", "why": [
                "counted on footage showing RetailNext's marks: " + (
                    "the test set is clean footage only" if which == "test"
                    else "clean footage only was asked for")]})
        elif (why := provisional(r)) and not include_provisional:
            left_out.append({"id": r["id"], "tier": "clean", "why": why})
    recs = [r for r in in_set if r["id"] not in {x["id"] for x in left_out}]
    if not recs:
        raise GoldError(f"No gold clip in the {which} set yet"
                        + (f" ({len(left_out)} clip(s) left out: see the gold page)."
                           if left_out else "."))
    tracking = tracking or bench.Tracking()
    results = [score_clip(r, root, tracking) for r in recs]
    scored = [c for c in results if c["scored"]]
    clean_windows = {c["window"] for c in scored if c["tier"] == "clean"}
    total: dict[str, dict[str, int]] = {}
    by_split: dict[str, dict[str, dict[str, int]]] = {}
    by_group: dict[str, dict[str, dict[str, int]]] = {}
    by_tier: dict[str, dict[str, dict[str, int]]] = {}
    twice = 0
    for c in scored:
        add(by_tier.setdefault(c["tier"], {}), c["by_direction"])
        if c["tier"] == "marked" and c["window"] in clean_windows:
            twice += 1  # the same window's clean count is in the totals already
            continue
        add(total, c["by_direction"])
        add(by_split.setdefault(c["split"], {}), c["by_direction"])
        for g in c["groups"]:
            add(by_group.setdefault(g, {}), c["by_direction"])
    # one score, one detector: a number over clips counted by different detectors (or
    # different weights) would say nothing about either
    used = {d for c in scored for d in detector_of(c)}
    if len(used) > 1:
        said = "; ".join(sorted(f"{b} {m} ({(s or '?')[:12]})" for b, m, s in used))
        raise GoldError(f"These clips' automatic counts used different detectors ({said}): a "
                        f"score over them would mix detectors. Run the automatic count again "
                        f"with one detector, or score the clips of each separately.")
    n = sum(v["truth"] for v in total.values())
    made = datetime.now().astimezone()
    ids = {r["id"] for r in recs}
    tiers = {k: sum(1 for c in scored if c["tier"] == k) for k in TIERS}
    notes = []
    provisional_out = [x for x in left_out if x["tier"] == "clean"]
    if provisional_out:
        notes.append(f"{len(provisional_out)} provisional clip(s) left out: not known to have been "
                     f"counted on clean footage.")
    marked_out = len(left_out) - len(provisional_out)
    if marked_out:
        notes.append(f"{marked_out} clip(s) counted on marked footage left out: "
                     + ("the test set is clean footage only." if which == "test"
                        else "clean footage only was asked for."))
    if tiers["marked"]:
        notes.append(f"{tiers['marked']} of the {len(scored)} scored clip(s) were counted on footage "
                     f"showing RetailNext's marks, which can sway a count towards the sensor's: "
                     f"the clean-footage row is the one to rely on.")
    if twice:
        notes.append(f"{twice} window(s) counted on both clean and marked footage: the totals "
                     f"count each once, from its clean count.")
    if not tracking.usual:
        notes.append(f"Tracking was replayed with {tracking.tracker} and the {tracking.assoc} "
                     f"association matrix, not the settings the tool counts with: an experiment "
                     f"in following people between frames, not a result for the product.")
    exp_id, k = f"{made:%Y%m%d-%H%M%S}-{which}", 2
    while (experiments_dir(root) / f"{exp_id}.json").exists():  # never over an earlier scoring
        exp_id, k = f"{made:%Y%m%d-%H%M%S}-{which}-{k}", k + 1
    exp = {"id": exp_id, "made_at": made.isoformat(timespec="seconds"),
           "set": which, "splits": list(chosen), "clean_only": clean_only,
           "dataset": {"id": DATASET, "version": version, "content_hash": current["content_hash"],
                       "clips": [{k: c.get(k) for k in ("id", "split", "tier", "sha256")}
                                 for c in current["clips"] if c["id"] in ids]},
           "app_version": app_version(),
           "settings": {**bench.settings(), "tracking": tracking.as_dict(),
                        "engine": CROSSING_ENGINE, "matching": MATCHING,
                        "tolerance_s": TOLERANCE_S, "min_sample": MIN_SAMPLE,
                        "ground_truth_specification": GROUND_TRUTH_SPEC},
           "detectors": sorted({d["model"]: d for c in scored for d in c["detectors"]}.values(),
                               key=lambda d: str(d["model"])),
           "clips": results, "totals": summary(total) if total else None, "tiers": tiers,
           "by_tier": {k: summary(v) for k, v in by_tier.items()},
           "by_split": {k: summary(v) for k, v in by_split.items()},
           "by_tag": {k: summary(v) for k, v in sorted(by_group.items())},
           "workload": _workload(scored), "note": note, "provisional_left_out": provisional_out,
           "left_out": left_out, "limits": _limits(recs, scored, n) + notes}
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
                    "app_version": e.get("app_version"), "clean_only": e.get("clean_only", True),
                    "tiers": e.get("tiers") or {"clean": sum(1 for c in e.get("clips", [])
                                                             if c.get("scored")), "marked": 0},
                    "dataset_version": ds.get("version") if isinstance(ds, dict) else ds,
                    "clips": sum(1 for c in e.get("clips", []) if c.get("scored")),
                    "crossings": tot.get("truth", 0), "recall": tot.get("recall"),
                    "precision": tot.get("precision"), "sufficient": tot.get("sufficient", False)})
    return out


# ---- how far RetailNext's marks sway a count -----------------------------------------------

def _reviewers(rec: dict[str, Any]) -> set[str]:
    return {str(r["reviewer"]).casefold() for r in rec["reviews"]}


def _counted_at(rec: dict[str, Any]) -> list[datetime]:
    return [datetime.fromisoformat(r["at"]) for r in rec["reviews"] if r.get("at")]


def marks_effect(root: Path | None = None, shared: Path | None = None) -> dict[str, Any]:
    """Windows counted on clean and on marked footage: the two counts matched crossing by
    crossing (as two people's counts are), each direction's count on both, and, where the
    system's own number for the window is known, whether the marked count moved towards it.
    Nothing is concluded from fewer than PAIRS_FOR_A_VERDICT windows."""
    by_window: dict[str, dict[str, dict[str, Any]]] = {}
    for rec in clips(root, shared):
        by_window.setdefault(window_id(rec["id"]), {})[tier(rec)] = rec
    pairs: list[dict[str, Any]] = []
    for window, both in sorted(by_window.items()):
        if set(both) != set(TIERS):
            continue
        c, m = both["clean"], both["marked"]
        row: dict[str, Any] = {"window": window, "store": c["store"], "period": c["period"],
                               "split": c["split"], "clean_id": c["id"], "marked_id": m["id"]}
        if (c["dirs"] != m["dirs"] or sorted(x["sensor"] for x in c["cameras"])
                != sorted(x["sensor"] for x in m["cameras"]) or c.get("rules") != m.get("rules")):
            pairs.append({**row, "comparable": False, "reason": (
                "not counted the same way (other traffic, cameras, or rules for children and "
                "staff)")})
            continue
        dirs, cams = list(c["dirs"]), [x["sensor"] for x in c["cameras"]]
        real_c, _, _ = truth(c)
        real_m, _, _ = truth(m)
        got = dict.fromkeys(("agreed", "direction_disagreements", "only_first", "only_second",
                             "disagreements"), 0)
        for cam in cams:
            g = agreement(real_c.get(cam, []), real_m.get(cam, []), dirs)
            for k in got:
                got[k] += int(g[k])
        system = next((r["system_counts"] for r in [*c["reviews"], *m["reviews"]]
                       if r.get("system_counts")), None)
        per_dir = {}
        for d in dirs:
            nc = sum(1 for cam in cams for _, dd in real_c.get(cam, []) if dd == d)
            nm = sum(1 for cam in cams for _, dd in real_m.get(cam, []) if dd == d)
            s = (system or {}).get("counts", {}).get(d)
            way = (None if s is None or nm == nc else
                   "towards" if abs(nm - s) < abs(nc - s) else
                   "away" if abs(nm - s) > abs(nc - s) else "neither")
            per_dir[d] = {"clean": nc, "marked": nm, "shift": nm - nc, "system": s,
                          "towards_system": way}
        same = sorted(_reviewers(c) & _reviewers(m))
        gap = min((abs((a - b).total_seconds()) / 86400 for a in _counted_at(c)
                   for b in _counted_at(m)), default=None)
        union = got["agreed"] + got["disagreements"]
        pairs.append({**row, "comparable": True, **got, "only_clean": got["only_first"],
                      "only_marked": got["only_second"],
                      "agreement_pct": round(100.0 * got["agreed"] / union, 1) if union else None,
                      "by_direction": per_dir, "system": (system or {}).get("system"),
                      "same_person": same, "days_apart": None if gap is None else round(gap, 1),
                      "memory_warning": bool(same) and (gap is None or gap < 7)})
    ok = [p for p in pairs if p["comparable"]]
    totals = {k: sum(p[k] for p in ok) for k in ("agreed", "direction_disagreements",
                                                 "only_clean", "only_marked", "disagreements")}
    union = totals["agreed"] + totals["disagreements"]
    shift = {d: sum(p["by_direction"][d]["shift"] for p in ok if d in p["by_direction"])
             for d in ("in", "out")}
    ways = [v["towards_system"] for p in ok for v in p["by_direction"].values()
            if v["towards_system"]]
    towards, away = ways.count("towards"), ways.count("away")
    if len(ok) < PAIRS_FOR_A_VERDICT:
        sentence = (f"{len(ok)} window(s) counted on both clean and marked footage: at least "
                    f"{PAIRS_FOR_A_VERDICT} are needed before anything is said about how far "
                    f"RetailNext's marks sway a count.")
    else:
        sentence = (f"Across {len(ok)} windows counted both ways, the count on marked footage "
                    f"matched the clean count on {round(100.0 * totals['agreed'] / union, 1) if union else '–'}% "
                    f"of crossings; it found {totals['only_marked']} the clean count did not and "
                    f"missed {totals['only_clean']} it did. Where the count moved and the "
                    f"system's number is known, it moved towards the system's number {towards} "
                    f"time(s) and away {away} time(s).")
    return {"pairs": pairs, "windows": len(ok), "needed": PAIRS_FOR_A_VERDICT, "totals": {
        **totals, "agreement_pct": round(100.0 * totals["agreed"] / union, 1) if union else None,
        "shift": shift, "towards_system": towards, "away_from_system": away},
        "memory_warnings": sum(1 for p in ok if p["memory_warning"]), "sentence": sentence}


def overview(root: Path | None = None, shared: Path | None = None) -> dict[str, Any]:
    recs = clips(root, shared)
    sets: dict[str, dict[str, Any]] = {s: {"clips": 0, "marked": 0, "stores": set(),
                                           "crossings": 0} for s in SPLITS}
    described = [describe(r) for r in recs]
    for d in described:
        s = sets[d["split"]]
        s["clips"] += 1
        s["marked"] += d["tier"] == "marked"
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
            "marks_effect": marks_effect(root, shared),
            "tolerance_s": TOLERANCE_S, "min_sample": MIN_SAMPLE,
            "specification": GROUND_TRUTH_SPEC}
