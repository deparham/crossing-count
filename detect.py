#!/usr/bin/env python3
"""M2 - candidate crossings.

    uv run detect.py VIDEO CONFIG [CONFIG ...] [--sample 120] [--naive | --compare]

Runs person detection and tracking inside gate.py's activity ranges and applies
each camera's counting rule. Writes, per camera:
  candidates.json   crossings the rule would count, for a person to confirm
  discarded.json    every track the rule rejected, with the reason
  unexplained.json  motion near the line that produced no proposal (likely misses)

Run gate.py first with the same video, configs and --sample. --naive uses YOLO
on the whole picture instead of de-rotated crops; --compare runs both and
prints them side by side (naive results go to <camera>/naive/).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from crossing_count.candidates import (
    DetectOptions,
    backbone_factory,
    fmt_summary,
    run_detect,
    write_detection,
)
from crossing_count.config import ConfigError
from crossing_count.detect_debug import write_detect_debug
from crossing_count.detector import Backbone, RfDetrBackbone, YoloBackbone
from crossing_count.util import default_run_dir


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("video", type=Path)
    ap.add_argument("configs", type=Path, nargs="+", metavar="CONFIG")
    ap.add_argument("--out-dir", type=Path, help="run directory (default: as gate.py)")
    ap.add_argument("--sample", type=float, metavar="SECONDS",
                    help="process only the first SECONDS (must match gate.py)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--naive", action="store_true", help="YOLO on the raw picture (baseline)")
    mode.add_argument("--compare", action="store_true", help="run de-rotated and naive, compare")
    ap.add_argument("--detector", choices=("yolo", "rfdetr"), default="yolo",
                    help="which detector finds the people (rfdetr: uv sync --group detectors).\n"
                         "Its results go to <camera>/<detector>-<mode>/, so detectors can be "
                         "compared on the same clip")
    ap.add_argument("--model", default="yolo11s.pt", help="weights file in models/")
    ap.add_argument("--rfdetr-weights", default="rf-detr-large-2026.pth",
                    help="RF-DETR Large weights file in models/")
    ap.add_argument("--device", help="torch device (default: mps if available, else cpu)")
    ap.add_argument("--det-fps", type=float, default=10.0, help="frames per second analysed")
    ap.add_argument("--seed", type=int, default=0,
                    help="accepted for a uniform interface; detection itself is not random")
    ap.add_argument("--debug", action="store_true",
                    help="write contact sheets of candidates, discards and unexplained motion")
    ap.add_argument("--record", action="store_true",
                    help="also save every frame's detections (detections.pkl) so tracking "
                         "settings can be re-tried without re-running YOLO")
    args = ap.parse_args(argv)

    if not args.video.is_file():
        ap.error(f"video not found: {args.video}")
    run_dir = args.out_dir or default_run_dir(args.video, args.sample)
    modes = ["derotated", "naive"] if args.compare else ["naive"] if args.naive else ["derotated"]

    try:
        backbone: Backbone = (YoloBackbone(args.model, args.device) if args.detector == "yolo"
                              else RfDetrBackbone(args.rfdetr_weights, args.device))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    by_mode = {}
    for m in modes:
        opts = DetectOptions(mode=m, model=args.model, device=args.device, det_fps=args.det_fps,
                             record=args.record, backbone=args.detector)
        where = m if args.detector == "yolo" else f"{args.detector}-{m}"
        print(f"\n[{args.detector} {m}]", file=sys.stderr)
        try:
            res = run_detect(args.video, args.configs, run_dir, opts=opts, sample_s=args.sample,
                             factory=backbone_factory(opts, backbone), progress=True)
        except (ConfigError, FileNotFoundError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        by_mode[m] = res
        for r in res:
            out = write_detection(r, run_dir, where)
            print(f"\n{r.cfg.sensor} [{args.detector} {m}]  rule: {r.cfg.rule}")
            print("\n".join(fmt_summary(r)))
            print(f"  wrote {out}/candidates.json, discarded.json, unexplained.json")
            if args.debug:
                for p in write_detect_debug(args.video, r.cfg, r.candidates, r.discarded,
                                            r.unexplained, out, tracks_json=r.tracks_json()):
                    print(f"  wrote {p}")

    if args.compare:
        print("\nDe-rotated vs naive (same clip, same tracker and rule)")
        keys = [("candidates", "candidates"), ("in", "in"), ("out", "out"),
                ("tracks", "tracks"), ("mean_detections_per_frame", "people/frame"),
                ("pending_expired", "pending_expired"), ("unexplained_ranges", "unexplained"),
                ("unexplained_no_person", "  nobody detected"), ("stitched_joins", "stitched")]
        for d, n in zip(by_mode["derotated"], by_mode["naive"]):
            sd, sn = d.candidates["summary"], n.candidates["summary"]
            print(f"\n  {d.cfg.sensor:<14} {'derotated':>10} {'naive':>10}")
            for k, label in keys:
                print(f"  {label:<18} {sd[k]!s:>10} {sn[k]!s:>10}")
            print(f"  {'wall time (s)':<18} {d.wall_s:>10.0f} {n.wall_s:>10.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
