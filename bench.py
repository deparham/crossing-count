#!/usr/bin/env python3
"""How many real crossings the automatic count finds, on clips a person has counted.

    uv run bench.py                   every clip in runs/ with a finished count by a person
    uv run bench.py VIDEO [VIDEO ...] just these clips
    uv run bench.py --allow-config-change
                                      score even if a drawing changed since the count ran

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

from crossing_count import paths
from crossing_count.bench import BenchError, bench_clip, benchmark_runs, settings, totals
from crossing_count.config import ConfigError
from crossing_count.util import default_run_dir, write_json_atomic


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{v:.0f}%"


def _dur(s: float) -> str:
    s = round(s)
    return f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"


def show_clip(c: dict[str, Any]) -> None:
    t = c["truth"]
    print(f"\n{c['clip']}\n  truth: {t['kind']} ({t['source']})")
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("videos", nargs="*", type=Path)
    ap.add_argument("--allow-config-change", action="store_true",
                    help="score cameras whose drawing changed since the count ran (on a copy)")
    args = ap.parse_args(argv)
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
    clips, skipped = [], []
    for run_dir, video in jobs:
        try:
            clips.append(bench_clip(run_dir, video, args.allow_config_change))
        except (BenchError, ConfigError) as e:
            hint = " (or pass --allow-config-change)" if isinstance(e, ConfigError) else ""
            skipped.append(f"{run_dir.name}: {e}{hint}")
    for c in clips:
        show_clip(c)
    hand, every = totals(clips, independent_only=True), totals(clips, independent_only=False)
    if hand["clips"]:
        show_totals("Crossing recall on hand counts", hand)
    else:
        print("\nNo full hand count among these clips, so crossing recall cannot be measured: "
              "count one clip by hand in the wizard (Manual) to get it.")
    show_totals("All clips", every)
    for s in skipped:
        print(f"skipped {s}")
    out = root / "bench" / f"{datetime.now().astimezone():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(out, {"made_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                            "settings": settings(), "clips": clips, "skipped": skipped,
                            "totals": {"hand_counts": hand, "all": every}})
    print(f"\nSaved {out}")
    return 0 if clips else 1


if __name__ == "__main__":
    sys.exit(main())
