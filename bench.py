#!/usr/bin/env python3
"""How many real crossings the automatic count finds, on clips a person has counted.

    uv run bench.py                   every clip in runs/ with a finished count by a person
    uv run bench.py VIDEO [VIDEO ...] just these clips
    uv run bench.py --allow-config-change
                                      score even if a drawing changed since the count ran
    uv run bench.py --tracker byte --tracker ocsort
                                      the same clips under two trackers, side by side
    uv run bench.py --gold development --tracker byte --tracker ocsort
                                      the same, on the gold clips, at crossing level

Each clip's recorded detections are replayed through today's tracking and counting rule
(seconds per clip; the run folder is only read), and every crossing the person verified is
put where the checker would meet it: counted by the tool, on the list of possible misses,
only inside movement the tool could not explain (found only if watched), or never shown.

Only a hand count that watched all the footage measures crossing recall: a check of the
tool's own output cannot contain crossings the tool never showed. Results are also saved
in bench/ in the data folder, to compare with later runs.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from crossing_count import gold, paths
from crossing_count.bench import (
    BenchError,
    Tracking,
    bench_clip,
    benchmark_runs,
    settings,
    totals,
    variants,
)
from crossing_count.config import ConfigError
from crossing_count.evaluate import MIN_SAMPLE
from crossing_count.tracker import ASSOC, TRACKERS
from crossing_count.util import default_run_dir, write_json_atomic


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{v:.0f}%"


def _dur(s: float) -> str:
    s = round(s)
    return f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"


def said_detector(d: dict[str, Any]) -> str:
    if not d:
        return "unknown detector"
    bits = [str(d.get("backbone") or "?"), str(d.get("model") or "")]
    if d.get("mode"):
        bits.append(str(d["mode"]))
    if d.get("weights_sha256"):
        bits.append(f"weights {str(d['weights_sha256'])[:12]}")
    return " ".join(b for b in bits if b)


def show_clip(c: dict[str, Any]) -> None:
    t = c["truth"]
    print(f"\n{c['clip']}\n  truth: {t['kind']} ({t['source']})")
    print(f"  detector: {said_detector(c.get('detector') or {})}")
    if t["partial"]:
        print("  (a hand count of part of the footage: shown here, left out of the totals)")
    elif not t["independent"]:
        print("  (not a full hand count: crossings the tool never showed anywhere cannot be in "
              "it; 'never shown' = shown when checked, but not by today's method)")
    for note in [*t["notes"], *c["notes"]]:
        print(f"  note: {note}")
    print(f"  {'camera':<14}{'dir':<5}{'verified':>9}{'counted':>9}{'real':>6}{'dup':>5}"
          f"{'wrong way':>10}{'false':>7}{'on list':>9}{'real':>6}{'in loop':>9}"
          f"{'by watching':>13}{'never shown':>13}")
    for cam, r in c["cameras"].items():
        for d, n in r["by_direction"].items():
            print(f"  {cam:<14}{d:<5}{n['verified']:>9}{n['counted']:>9}{n['counted_real']:>6}"
                  f"{n['duplicates']:>5}{n['wrong_direction']:>10}{n['false']:>7}"
                  f"{n['check_list']:>9}{n['check_list_real']:>6}{n['in_loop']:>9}"
                  f"{n['watching']:>13}{n['never_shown']:>13}")
            if n["never_shown_at"]:
                print(f"  {'':<19}never shown at (s): {', '.join(map(str, n['never_shown_at']))}")


def show_totals(title: str, tot: dict[str, Any]) -> None:
    print(f"\n{title} ({tot['clips']} clip(s)):")
    for d, n in tot["by_direction"].items():
        print(f"  {d.upper():<4} {n['verified']} verified. Counted by the tool "
              f"{n['counted_real']} ({_pct(n['recall_counted'])}); with the possible-miss list "
              f"and everyone in each loop {n['counted_real'] + n['check_list_real'] + n['in_loop']}"
              f" ({_pct(n['recall_checked'])}); with watching "
              f"{n['verified'] - n['never_shown']} ({_pct(n['recall_shown'])}); "
              f"never shown {n['never_shown']}.")
        print(f"       Counts: {n['counted']}, real {n['counted_real']} "
              f"({_pct(n['precision_counted'])}); same person twice {n['duplicates']}, "
              f"wrong direction {n['wrong_direction']}, nobody {n['false']}.")
    print(f"  Work: {tot['check_items']} Y/N questions, {_dur(tot['watch_s'])} of movement "
          f"to watch.")
    if tot.get("camera_hours"):
        print(f"  Per camera-hour of footage ({tot['camera_hours']} h): "
              f"{tot['items_per_camera_hour']} questions, "
              f"{tot['review_minutes_per_camera_hour']} minutes of checking, "
              f"{tot['watch_share_pct']}% of the footage left as movement to watch.")
    if tot.get("static_tracks"):
        print(f"  Things, not people: {tot['static_tracks']} track(s) that never moved "
              f"(mannequins, racks, posters) made {tot['static_items']} question(s), "
              f"{tot['static_items_per_camera_hour']} per camera-hour. A camera surrounded by "
              f"them needs an exclusion zone.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("videos", nargs="*", type=Path)
    ap.add_argument("--allow-config-change", action="store_true",
                    help="score cameras whose drawing changed since the count ran (on a copy)")
    ap.add_argument("--detector", action="append", metavar="SET", dest="detectors",
                    help="which recorded detection set to score: derotated (the default, YOLO),\n"
                         "naive, rfdetr-derotated, ... Repeat to compare detectors on the same "
                         "clips")
    ap.add_argument("--all-detectors", action="store_true",
                    help="score every recorded detection set found, and compare them")
    ap.add_argument("--tracker", action="append", choices=TRACKERS, dest="trackers",
                    help="which tracker follows people between frames: byte (the default),\n"
                         "botsort, ocsort. Repeat to compare trackers on the same clips.\n"
                         "Detection is not re-run: only the tracking changes")
    ap.add_argument("--gold", choices=("development", "test"), metavar="SET",
                    help="score the gold clips (development or test) instead of the run\n"
                         "folders, once per way of tracking. Each scoring is kept as its own\n"
                         "experiment record, which names the tracking it used")
    ap.add_argument("--assoc", action="append", choices=ASSOC, dest="assocs",
                    help="what the first association is measured on: iou (the default) or\n"
                         "giou, which still ranks boxes that do not overlap at all\n"
                         "(with --tracker byte only)")
    args = ap.parse_args(argv)
    ways = [Tracking(t, a) for t in (args.trackers or ["byte"]) for a in (args.assocs or ["iou"])]
    for w in ways:
        if w.assoc == "giou" and w.tracker != "byte":
            ap.error(f"--assoc giou goes with --tracker byte: {w.tracker} adds its own terms "
                     f"to the matrix, which this would drop")
    if args.gold:
        return score_gold(args.gold, ways)
    root = paths.data_root()
    jobs: list[tuple[Path, Path | None]]
    if args.videos:
        jobs = [(root / default_run_dir(v, None), v) for v in args.videos]
    else:
        runs = root / "runs"
        jobs = [(d, None) for d in benchmark_runs(runs)] if runs.is_dir() else []
    if not jobs:
        print("No clip has a finished count by a person yet. Count one by hand in the wizard "
              "(Manual), or check an automatic count, then run this again.")
        return 2
    wanted = args.detectors or (sorted({v for d, _ in jobs for v in variants(d)})
                                if args.all_detectors else ["derotated"])
    by_set: dict[str, dict[str, Any]] = {}
    clips, skipped = [], []
    for variant in wanted or ["derotated"]:
        for way in ways:
            name = way.label(variant)
            got: list[dict[str, Any]] = []
            for run_dir, video in jobs:
                try:
                    got.append(bench_clip(run_dir, video, args.allow_config_change, variant, way))
                except (BenchError, ConfigError, ValueError) as e:
                    hint = " (or pass --allow-config-change)" if isinstance(e, ConfigError) else ""
                    skipped.append(f"{run_dir.name} [{name}]: {e}{hint}")
            clips += got
            for c in got:
                show_clip(c)
            hand, every = totals(got, independent_only=True), totals(got, independent_only=False)
            if hand["clips"]:
                show_totals(f"Crossing recall on hand counts [{name}]", hand)
            else:
                print(f"\nNo full hand count among these clips [{name}], so crossing recall "
                      f"cannot be measured: count one clip by hand in the wizard (Manual) to "
                      f"get it.")
            show_totals(f"All clips [{name}]", every)
            by_set[name] = {"hand_counts": hand, "all": every, "tracking": way.as_dict(),
                            "detector": next((c["detector"] for c in got if c.get("detector")), {})}
    if len(by_set) > 1:
        compare(by_set)
    for s in skipped:
        print(f"skipped {s}")
    out = root / "bench" / f"{datetime.now().astimezone():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(out, {"made_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "settings": settings(), "clips": clips, "skipped": skipped,
                            "detectors": {k: v["detector"] for k, v in by_set.items()},
                            "tracking": {k: v["tracking"] for k, v in by_set.items()},
                            "totals": {k: {"hand_counts": v["hand_counts"], "all": v["all"]}
                                       for k, v in by_set.items()}})
    print(f"\nSaved {out}")
    return 0 if clips else 1


def score_gold(which: str, ways: list[Tracking]) -> int:
    """The gold clips under each way of following people between frames. Each scoring is kept
    as its own experiment record, and one made with anything but the settings the tool counts
    with says so in its own limits."""
    print(f"\nGold clips [{which}] (crossing level)")
    print(f"  {'tracking':<20}{'clips':>6}{'verified':>9}{'found':>7}{'false':>7}{'recall':>8}"
          f"{'precision':>11}")
    kept: list[tuple[str, dict[str, Any]]] = []
    limits: list[str] = []
    for way in ways:
        name = f"{way.tracker}/{way.assoc}" + ("" if not way.usual else " (as counted)")
        try:
            exp = gold.evaluate(which, note=f"tracking: {way.tracker}/{way.assoc}", tracking=way)
        except gold.GoldError as e:
            print(f"  {name:<20}{e}")
            continue
        tot = (exp.get("totals") or {}).get("all") or {}
        if not tot:
            print(f"  {name:<20}no clip could be scored")
            continue
        kept.append((name, exp))
        # each row says its own tracking already; the rest of the limits are the set's, and
        # the same whichever tracker ran
        limits = limits or [x for x in exp["limits"] if not x.startswith("Tracking was replayed")]
        print(f"  {name:<20}{sum(exp['tiers'].values()):>6}{tot['truth']:>9}{tot['found']:>7}"
              f"{tot['false']:>7}{_pct(tot['recall']):>8}{_pct(tot['precision']):>11}")
    for line in limits:
        print(f"  limit (every row): {line}")
    for name, exp in kept:
        print(f"  {name}: kept as {exp['id']}")
    return 0 if kept else 1


def compare(by_set: dict[str, dict[str, Any]]) -> None:
    """Detectors and trackers side by side on the same clips, at crossing level. No winner is
    declared here: with few crossings the difference means nothing, and it says so."""
    print("\nOn the same clips (crossing level, hand counts only)")
    print(f"  {'scored as':<28}{'verified':>9}{'counted':>9}{'recall':>8}{'precision':>11}"
          f"{'questions/h':>13}{'static/h':>10}")
    enough = True
    for name, v in by_set.items():
        tot = v["hand_counts"]
        n = sum(x["verified"] for x in tot["by_direction"].values()) if tot["by_direction"] else 0
        counted = sum(x["counted_real"] for x in tot["by_direction"].values())
        recall = _pct(round(100.0 * counted / n, 1)) if n else "–"
        prec = sum(x["counted"] for x in tot["by_direction"].values())
        enough = enough and n >= MIN_SAMPLE
        print(f"  {name:<28}{n:>9}{counted:>9}{recall:>8}"
              f"{_pct(round(100.0 * counted / prec, 1)) if prec else '–':>11}"
              f"{tot.get('items_per_camera_hour') or '–'!s:>13}"
              f"{tot.get('static_items_per_camera_hour') or '–'!s:>10}")
    if not enough:
        print(f"  Too few verified crossings to tell these apart (at least {MIN_SAMPLE} per "
              f"row, on clips counted fully by hand). These are counts, not a result.")
    for name, v in by_set.items():
        print(f"  {name}: {said_detector(v['detector'])}")


if __name__ == "__main__":
    sys.exit(main())
