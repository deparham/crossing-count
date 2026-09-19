"""Benchmark: how many of the crossings a person verified the automatic count finds.

Each clip's recorded detections are replayed through today's tracking and counting rule
(no detector run: seconds per clip), and every verified crossing is put where the checker
would meet it:

    counted       counted by the tool (a Y/N question)
    check list    not counted, but listed as a possible miss (a Y/N question)
    in a loop     not asked about, but crossing inside a question's loop: counted when the
                  checker answers with the number of people who crossed there
    watching      only inside movement the tool could not explain: found only if watched
    never shown   nowhere the tool pointed: only counting the footage by hand finds these

Crossing recall is the share found by the first group (then the first two, the first
three). It can only be measured against a count the tool did not shape: a hand count that
watched all the footage. A check of the tool's own output holds only crossings the tool
showed somewhere, so on those clips a crossing the tool never shows cannot be in the truth;
"never shown" there means shown when the clip was checked, but not by today's method.
"""

from __future__ import annotations

import csv
import json
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import evaluate, paths
from .candidates import CameraDetection, DetectOptions, output_dir, replay_factory, run_detect
from .config import load_config
from .export import CSV_COLUMNS
from .gating import camera_dir
from .manual import merge_ranges
from .version import app_version
from .wizard import (
    CHOICES,
    CLIP_AFTER_S,
    CLIP_BEFORE_S,
    DIRECTIONS,
    MIN_WATCHED_PCT,
    POSSIBLE_REASONS,
    default_folders,
    review_items,
    watch_stretches,
)

WINDOW_S = 2.0  # a proposal and a verified crossing this close (camera, direction) are one
WATCH_MARGIN_S = 1.0  # a crossing this close to a stretch of movement is seen when watching it
SECONDS_PER_ITEM = 7.5  # a Y/N question, when the review was not timed (measured: 190 in ~24 min)
IDLE_GAP_S = 120.0  # a gap this long between answers is a break, not reviewing
STATIC_MOVE_FRAC = 0.03  # a track that never moves further than this (picture heights) ...
STATIC_MIN_S = 5.0  # ... for this long is a thing, not a person: a mannequin, a rack, a poster
HAND = "hand count"
CHECKED = "checked"
FIELDS = ("verified", "counted", "counted_real", "duplicates", "wrong_direction", "false",
          "check_list", "check_list_real", "in_loop", "watching", "never_shown")


class BenchError(Exception):
    """A clip that cannot be scored."""


@dataclass(frozen=True)
class Tracking:
    """How the recorded detections are followed: the default is what the tool counts with."""

    tracker: str = "byte"
    assoc: str = "iou"

    def options(self) -> DetectOptions:
        return DetectOptions(tracker=self.tracker, assoc=self.assoc)

    @property
    def usual(self) -> bool:
        return (self.tracker, self.assoc) == (Tracking().tracker, Tracking().assoc)

    def label(self, variant: str) -> str:
        """What to call this way of scoring a clip, in tables and in saved results."""
        return variant if self.usual else f"{variant} [{self.tracker}/{self.assoc}]"

    def as_dict(self) -> dict[str, str]:
        return {"tracker": self.tracker, "assoc": self.assoc}


@dataclass
class Truth:
    """The crossings a person verified on one clip, and how they were found."""

    kind: str  # HAND: every moment watched; CHECKED: a check of what the tool showed
    source: str
    dirs: list[str]
    crossings: dict[str, list[tuple[float, str]]]  # camera -> [(seconds, direction)]
    video: str | None = None  # the video's full path, where the source keeps it
    notes: list[str] = field(default_factory=list)

    @property
    def independent(self) -> bool:
        return self.kind == HAND


def _json(p: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _watched_pct(ranges: list[list[float]], duration: float) -> float:
    return 100.0 * sum(b - a for a, b in merge_ranges(ranges)) / duration if duration else 0.0


def _wizard_hand(st: dict[str, Any]) -> Truth:
    m, dur = st["manual"], float(st["duration_s"])
    cams = [c["sensor"] for c in st["cameras"]]
    notes = [f"{c}: only {pct:.0f}% of the footage was watched" for c in cams
             if (pct := _watched_pct(m["watched"].get(c, []), dur)) < MIN_WATCHED_PCT]
    return Truth(HAND if not notes else f"{HAND} (partial)", "wizard, counted by hand",
                 CHOICES[st["direction"]],
                 {c: [(float(x["t"]), x["direction"]) for x in m["counts"] if x["camera"] == c]
                  for c in cams}, st.get("video"), notes)


def _counter_hand(hand: dict[str, Any]) -> Truth:
    """count.py's hand count: every direction that was counted at all."""
    dur = float(hand["duration_s"])
    names = {int(c["index"]): str(c["name"]) for c in hand["cameras"]}
    crossings: dict[str, list[tuple[float, str]]] = {n: [] for n in names.values()}
    for c in hand["counts"]:
        crossings[names[int(c["camera"])]].append((float(c["t"]), c["direction"]))
    notes = [f"{n}: only {pct:.0f}% of the footage was watched" for i, n in names.items()
             if (pct := _watched_pct(hand["watched"].get(str(i), []), dur)) < MIN_WATCHED_PCT]
    dirs = [d for d in DIRECTIONS if any(c["direction"] == d for c in hand["counts"])]
    return Truth(HAND if not notes else f"{HAND} (partial)", "hand counter (count.py)", dirs,
                 crossings, None, notes)


def _wizard_check(run_dir: Path, st: dict[str, Any]) -> Truth:
    dirs, dur = CHOICES[st["direction"]], float(st["duration_s"])
    answers, watched = st.get("answers", {}), set(st.get("watched", []))
    crossings: dict[str, list[tuple[float, str]]] = {c["sensor"]: [] for c in st["cameras"]}
    unanswered = unsure = unwatched = 0
    for cam in st["cameras"]:
        d = camera_dir(run_dir, cam["sensor"])
        cand, disc, unex = (_json(d / f"{n}.json") or {}
                            for n in ("candidates", "discarded", "unexplained"))
        for it in review_items(cam["sensor"], cam["picture"], cand, disc, unex, dirs, dur):
            a = answers.get(it["id"])
            if a == "yes":
                crossings[cam["sensor"]].append((it["t"], it["direction"]))
            unanswered += a is None
            unsure += a == "unsure"
        unwatched += sum(r["id"] not in watched
                         for r in watch_stretches(cam["sensor"], cam["picture"], unex))
    for a in st.get("added", []):
        if a["camera"] in crossings and a["direction"] in dirs:
            crossings[a["camera"]].append((float(a["t"]), a["direction"]))
    notes = [text for n, text in ((unanswered, f"{unanswered} crossing(s) never answered"),
                                  (unsure, f"{unsure} unsure answer(s) left out"),
                                  (unwatched, f"{unwatched} stretch(es) of movement not watched"))
             if n]
    return Truth(CHECKED, "wizard check", dirs, crossings, st.get("video"), notes)


def _review_check(run_dir: Path) -> Truth | None:
    """review.py's verified crossings, as exported per camera."""
    crossings: dict[str, list[tuple[float, str]]] = {}
    for p in sorted((run_dir / "export").glob("*.csv")):
        rows = list(csv.reader(p.open(encoding="utf-8")))
        if CSV_COLUMNS not in rows:
            continue
        i = rows.index(CSV_COLUMNS)
        meta = {r[0]: r[1] for r in rows[:i] if len(r) >= 2}
        if meta.get("rule") == "manual count" or not meta.get("sensor"):
            continue
        crossings[meta["sensor"]] = [(float(r[1]), r[3]) for r in rows[i + 1:]
                                     if len(r) >= 4 and r[1]]
    if not crossings:
        return None
    return Truth(CHECKED, "review.py check", list(DIRECTIONS), crossings, None,
                 ["review.py does not record how much movement was watched"])


def find_truth(run_dir: Path) -> Truth | None:
    """The best count by a person saved in this run folder: a hand count first."""
    st = _json(run_dir / "wizard" / "state.json")
    if st and st.get("mode") == "manual" and st.get("manual", {}).get("done"):
        return _wizard_hand(st)
    hand = _json(run_dir / "manual" / "counts.json")
    if hand and hand.get("counts") and hand.get("cameras"):
        return _counter_hand(hand)
    if st and st.get("mode") != "manual" and st.get("job", {}).get("status") == "done":
        return _wizard_check(run_dir, st)
    return _review_check(run_dir)


def benchmark_runs(runs_root: Path) -> list[Path]:
    """Run folders holding a finished count by a person."""
    return [d for d in sorted(runs_root.iterdir()) if d.is_dir() and find_truth(d) is not None]


def _activities(run_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    return [(p.parent, a) for p in sorted(run_dir.glob("*/activity.json"))
            if (a := _json(p)) is not None]


def locate_video(run_dir: Path, truth: Truth) -> Path:
    if truth.video and Path(truth.video).is_file():
        return Path(truth.video)
    names = sorted({a["video"]["filename"] for _, a in _activities(run_dir) if "video" in a})
    for folder in default_folders():
        for name in names:
            if (folder / str(name)).is_file():
                return folder / str(name)
    raise BenchError(f"video not found ({', '.join(names) or 'unknown'}); give its path")


def variants(run_dir: Path) -> list[str]:
    """Every recorded detection set in this run: "derotated" (the camera's own folder) and
    any other detector or mode kept beside it, e.g. "rfdetr-derotated"."""
    found = set()
    for cam_dir, _ in _activities(run_dir):
        if (cam_dir / "detections.pkl").is_file():
            found.add("derotated")
        found |= {d.name for d in cam_dir.iterdir()
                  if d.is_dir() and (d / "detections.pkl").is_file()}
    return sorted(found)


def detector_of(run_dir: Path, variant: str = "derotated") -> dict[str, Any]:
    """Which detector and weights produced a recorded set."""
    for cam_dir, _ in _activities(run_dir):
        cand = _json(output_dir(run_dir, cam_dir.name, variant) / "candidates.json")
        if cand and cand.get("detector"):
            d = dict(cand["detector"])
            return {k: d.get(k) for k in ("backbone", "model", "mode", "conf", "imgsz",
                                          "resolution", "weights", "weights_sha256", "device",
                                          "library") if d.get(k) is not None}
    return {}


def _configs(run_dir: Path, sensors: set[str], variant: str = "derotated") -> list[Path]:
    """The drawing each camera was counted with, for cameras with recorded detections."""
    out: list[Path] = []
    for cam_dir, act in _activities(run_dir):
        if not (output_dir(run_dir, cam_dir.name, variant) / "detections.pkl").is_file():
            continue
        p = Path(str(act.get("config", {}).get("path", "")))
        found = next((c for c in (p, paths.data_root() / p, paths.SOURCE_ROOT / p)
                      if str(p) and c.is_file()), None)
        if found is None:
            raise BenchError(f"{cam_dir.name}: its drawing {p} is gone")
        if load_config(found).sensor in sensors:
            out.append(found)
    return out


def replay(video: Path, run_dir: Path, configs: list[str | Path], allow_config_change: bool = False,
           variant: str = "derotated",
           tracking: Tracking | None = None) -> tuple[list[CameraDetection], list[str]]:
    """Today's tracking and rule on the recorded detections. The run folder is only read.

    A drawing changed since the count ran stops the replay, unless allow_config_change:
    then it runs on a copy, assuming the change does not move where people were looked for.
    Another tracker or association matrix replays the same detections under that setting
    instead, which is how the two are compared on a clip counted by hand.
    """
    opts = (tracking or Tracking()).options()
    if not allow_config_change:
        return run_detect(video, configs, run_dir, opts=opts,
                          factory=replay_factory(run_dir, variant)), []
    notes: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="bench-"))
    try:
        for p in configs:
            cfg = load_config(p)
            src, dst = camera_dir(run_dir, cfg.sensor), camera_dir(tmp, cfg.sensor)
            dst.mkdir(parents=True)
            act = json.loads((src / "activity.json").read_text(encoding="utf-8"))
            if act["config"]["sha256"] != cfg.sha256:
                notes.append(f"{cfg.sensor}: the drawing changed since the count ran")
                act["config"]["sha256"] = cfg.sha256
            (dst / "activity.json").write_text(json.dumps(act), encoding="utf-8")
            kept = output_dir(tmp, cfg.sensor, variant)
            kept.mkdir(parents=True, exist_ok=True)
            shutil.copy2(output_dir(run_dir, cfg.sensor, variant) / "detections.pkl",
                         kept / "detections.pkl")
        return run_detect(video, configs, tmp, opts=opts,
                          factory=replay_factory(tmp, variant)), notes
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _pairs(a: list[float], b: list[float]) -> list[tuple[int, int]]:
    """One-to-one matches within WINDOW_S, by the one matching the whole project uses
    (evaluate.match, as the gold set is scored with): two scorers that disagreed about the
    same clip would be worse than either."""
    return [(j, i) for i, j in evaluate.match(b, a, WINDOW_S)]


def score_camera(real: list[tuple[float, str]], items: list[dict[str, Any]],
                 stretches: list[dict[str, Any]], dirs: list[str]) -> dict[str, dict[str, Any]]:
    """Each verified crossing by where the checker meets it; each wrong count by why."""
    out: dict[str, dict[str, Any]] = {}
    for d in dirs:
        truth = sorted(t for t, x in real if x == d)
        other = [t for t, x in real if x != d]
        counted = sorted(float(i["t"]) for i in items if i["kind"] == "counted" and i["direction"] == d)
        listed = sorted(float(i["t"]) for i in items if i["kind"] == "possible" and i["direction"] == d)
        hit = _pairs(counted, truth)
        got = {j for _, j in hit}
        left = [j for j in range(len(truth)) if j not in got]
        hit_list = _pairs(listed, [truth[j] for j in left])
        on_list = {left[j] for _, j in hit_list}
        rest = [j for j in left if j not in on_list]
        loops = [i.get("clip") or [float(i["t"]) - CLIP_BEFORE_S, float(i["t"]) + CLIP_AFTER_S]
                 for i in items if i["direction"] == d]
        in_loop = [j for j in rest if any(a <= truth[j] <= b for a, b in loops)]
        rest = [j for j in rest if j not in in_loop]
        seen = [j for j in rest if any(s["start"] - WATCH_MARGIN_S <= truth[j]
                                       <= s["end"] + WATCH_MARGIN_S for s in stretches)]
        never = [j for j in rest if j not in seen]
        used = {i for i, _ in hit}
        wrong = [t for i, t in enumerate(counted) if i not in used]
        dup = [t for t in wrong if any(abs(t - truth[j]) <= WINDOW_S for j in got)]
        other_wrong = [t for t in wrong if not any(abs(t - truth[j]) <= WINDOW_S for j in got)]
        wrong_way = [t for t in other_wrong if any(abs(t - o) <= WINDOW_S for o in other)]
        out[d] = {"verified": len(truth), "counted": len(counted), "counted_real": len(hit),
                  "duplicates": len(dup), "wrong_direction": len(wrong_way),
                  "false": len(other_wrong) - len(wrong_way),
                  "check_list": len(listed), "check_list_real": len(hit_list),
                  "in_loop": len(in_loop), "watching": len(seen), "never_shown": len(never),
                  "in_loop_at": [round(truth[j], 2) for j in in_loop],
                  "watching_at": [round(truth[j], 2) for j in seen],
                  "never_shown_at": [round(truth[j], 2) for j in never]}
    return out


def rates(n: dict[str, Any]) -> dict[str, float | None]:
    """Crossing recall three ways (counted; shown for Y/N; shown at all) and precision."""
    def pct(x: int, of: int) -> float | None:
        return round(100.0 * x / of, 1) if of else None

    v, found = n["verified"], n["counted_real"]
    asked = found + n["check_list_real"] + n["in_loop"]
    return {"recall_counted": pct(found, v),
            "recall_checked": pct(asked, v),
            "recall_shown": pct(asked + n["watching"], v),
            "precision_counted": pct(found, n["counted"])}


def static_objects(r: CameraDetection, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Tracks that never move: a mannequin, a rack, a poster - not a person.

    They are their own kind of false positive, and their own fix: a camera surrounded by
    human-shaped merchandise needs an exclusion zone, which is a finding for the customer,
    not a weakness of the model.
    """
    tile = (r.candidates.get("picture") or {}).get("tile") or {}
    height = float(tile.get("y1", 0)) - float(tile.get("y0", 0)) or 1.0
    limit = STATIC_MOVE_FRAC * height
    still = []
    for t in r.tracks:
        if len(t.samples) < 2 or t.samples[-1].t - t.samples[0].t < STATIC_MIN_S:
            continue
        x0, y0 = t.samples[0].x, t.samples[0].y
        if max(abs(s.x - x0) + abs(s.y - y0) for s in t.samples) <= limit:
            still.append(t)
    ids = {t.track_id for t in still}
    return {"tracks": len(still),
            "seconds": round(sum(t.samples[-1].t - t.samples[0].t for t in still), 1),
            "items": sum(1 for i in items if i.get("track_id") in ids
                         or set(i.get("tracks") or []) & ids)}


def review_minutes(run_dir: Path, items: int) -> tuple[float, str]:
    """How long the checking took: measured from the answers' own times where they were
    recorded, otherwise estimated from the number of questions."""
    st = _json(run_dir / "wizard" / "state.json") or {}
    stamps = sorted(str(d["at"]) for d in st.get("decisions", [])
                    if d.get("action") in ("answer", "add", "undo", "watched", "hand_count_added")
                    and d.get("at"))
    if len(stamps) >= 5:
        times = [datetime.fromisoformat(s) for s in stamps]
        worked = sum((b - a).total_seconds() for a, b in zip(times, times[1:], strict=False)
                     if (b - a).total_seconds() <= IDLE_GAP_S)
        if worked > 0:
            return round(worked / 60, 1), "measured from the answers' times"
    return round(items * SECONDS_PER_ITEM / 60, 1), f"estimated at {SECONDS_PER_ITEM:g} s a question"


def bench_clip(run_dir: Path, video: Path | None = None, allow_config_change: bool = False,
               variant: str = "derotated", tracking: Tracking | None = None) -> dict[str, Any]:
    truth = find_truth(run_dir)
    if truth is None:
        raise BenchError("no finished count by a person in this run")
    video = video or locate_video(run_dir, truth)
    configs: list[str | Path] = [*_configs(run_dir, set(truth.crossings), variant)]
    if not configs:
        raise BenchError(f"no recorded detections for {', '.join(truth.crossings)}"
                         f"{'' if variant == 'derotated' else f' [{variant}]'}: run the "
                         f"automatic count on this video first")
    tracking = tracking or Tracking()
    results, notes = replay(video, run_dir, configs, allow_config_change, variant, tracking)
    cams: dict[str, dict[str, Any]] = {}
    footage_s = 0.0
    for r in results:
        sensor = r.cfg.sensor
        pic = int(r.candidates.get("picture", {}).get("index", 0))
        items = review_items(sensor, pic, r.candidates, r.discarded, r.unexplained, truth.dirs,
                             float("inf"))
        stretches = watch_stretches(sensor, pic, r.unexplained)
        duration = float(r.candidates.get("processed_duration_s") or 0.0)
        footage_s += duration
        cams[sensor] = {"by_direction": score_camera(truth.crossings[sensor], items, stretches,
                                                     truth.dirs),
                        "check_items": len(items),
                        "watch_s": round(sum(s["end"] - s["start"] for s in stretches), 1),
                        "footage_s": duration, "static": static_objects(r, items)}
    notes += [f"{c}: no recorded detections, not scored" for c in truth.crossings if c not in cams]
    minutes, how = review_minutes(run_dir, sum(c["check_items"] for c in cams.values()))
    return {"clip": run_dir.name, "video": str(video), "variant": variant,
            "tracking": tracking.as_dict(), "scored_as": tracking.label(variant),
            "detector": detector_of(run_dir, variant),
            "truth": {"kind": truth.kind, "source": truth.source, "independent": truth.independent,
                      "partial": truth.kind != HAND and truth.kind.startswith(HAND),
                      "dirs": truth.dirs, "notes": truth.notes},
            "cameras": cams, "footage_s": round(footage_s, 1),
            "review_minutes": minutes, "review_minutes_how": how, "notes": notes}


def totals(clips: list[dict[str, Any]], independent_only: bool) -> dict[str, Any]:
    """Sums over clips. A hand count of part of the footage is never summed: whatever the
    tool counted in the unwatched part would look wrong."""
    by_dir: dict[str, dict[str, Any]] = {}
    check_items, watch_s, n = 0, 0.0, 0
    footage_s, minutes, static_tracks, static_items = 0.0, 0.0, 0, 0
    for clip in clips:
        t = clip["truth"]
        if t.get("partial") or (independent_only and not t["independent"]):
            continue
        n += 1
        minutes += float(clip.get("review_minutes") or 0.0)
        for cam in clip["cameras"].values():
            check_items += cam["check_items"]
            watch_s += cam["watch_s"]
            footage_s += float(cam.get("footage_s") or 0.0)
            static_tracks += int((cam.get("static") or {}).get("tracks", 0))
            static_items += int((cam.get("static") or {}).get("items", 0))
            for d, counts in cam["by_direction"].items():
                acc = by_dir.setdefault(d, dict.fromkeys(FIELDS, 0))
                for k in FIELDS:
                    acc[k] += counts[k]
    for acc in by_dir.values():
        acc.update(rates(acc))
    hours = footage_s / 3600

    def per_hour(x: float) -> float | None:
        return round(x / hours, 1) if hours else None

    # what the service costs to run: this decides whether a clip can be validated at all
    return {"clips": n, "by_direction": by_dir, "check_items": check_items,
            "watch_s": round(watch_s, 1), "camera_hours": round(hours, 2),
            "review_minutes": round(minutes, 1),
            "items_per_camera_hour": per_hour(check_items),
            "review_minutes_per_camera_hour": per_hour(minutes),
            "watch_share_pct": round(100.0 * watch_s / footage_s, 1) if footage_s else None,
            "static_tracks": static_tracks, "static_items": static_items,
            "static_items_per_camera_hour": per_hour(static_items)}


def settings() -> dict[str, Any]:
    """What the scores depend on, saved with them so runs can be compared."""
    return {"app_version": app_version(), "window_s": WINDOW_S, "watch_margin_s": WATCH_MARGIN_S,
            "possible_reasons": list(POSSIBLE_REASONS),
            "detect_options": json.loads(json.dumps(asdict(DetectOptions()), default=str))}
