#!/usr/bin/env python3
"""M1 - motion gating.

    uv run gate.py VIDEO CONFIG [CONFIG ...] [--sample 120] [--debug]

Finds the time ranges with any motion near each camera's counting line and
writes one activity.json per camera. For a multi-camera export, pass one
config per camera; each is matched to its picture by its burned-in line
(or use --tile SENSOR=N). The gate is biased hard toward keeping time; M2
only looks inside these ranges.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from crossing_count.config import ConfigError
from crossing_count.gating import camera_dir, run_gate, write_debug, write_outputs
from crossing_count.util import default_run_dir, fmt_hms

_OVERRIDES = ("line_margin", "warmup_s", "absorb_s", "var_threshold", "min_blob_frac",
              "pre_roll_s", "post_roll_s", "merge_gap_s", "proc_fps")


def _tile_map(values: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        sensor, _, idx = v.rpartition("=")
        if not sensor or not idx.isdigit():
            raise argparse.ArgumentTypeError(f"--tile expects SENSOR=N, got {v!r}")
        out[sensor] = int(idx)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("video", type=Path)
    ap.add_argument("configs", type=Path, nargs="+", metavar="CONFIG")
    ap.add_argument("--tile", action="append", default=[], metavar="SENSOR=N",
                    help="assign a config to picture N (0 = top-left) instead of auto-matching")
    ap.add_argument("--out-dir", type=Path, help="default: runs/<video>[__sampleN]/")
    ap.add_argument("--sample", type=float, metavar="SECONDS",
                    help="process only the first SECONDS of video")
    ap.add_argument("--debug", action="store_true",
                    help="write gate-region, timeline and spot-check images per camera")
    ap.add_argument("--seed", type=int, default=0, help="seed for debug spot-check sampling")
    for name in _OVERRIDES:
        ap.add_argument(f"--{name.replace('_', '-')}", type=float,
                        help="override this gate parameter for this run")
    args = ap.parse_args(argv)

    if not args.video.is_file():
        ap.error(f"video not found: {args.video}")
    try:
        tile_map = _tile_map(args.tile)
        run = run_gate(args.video, args.configs, tile_map=tile_map, sample_s=args.sample,
                       overrides={k: getattr(args, k) for k in _OVERRIDES}, progress=True)
    except (ConfigError, argparse.ArgumentTypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    out_dir = args.out_dir or default_run_dir(args.video, args.sample)
    written = write_outputs(run, out_dir)

    tb = run.audit
    print(f"\n{run.info.filename}")
    print(f"  {run.info.width}x{run.info.height} {run.info.codec}, {tb.effective_fps:.3f} fps "
          f"from timestamps, {tb.n_frames} frames, {fmt_hms(tb.duration_s)}")
    for w in tb.warnings:
        print(f"  WARNING: {w}")
    print(f"  {len(run.tiles)} camera picture(s): " + "; ".join(
        f"#{t.index} {t.width}x{t.height} at ({t.x0},{t.y0})"
        + (f", header {t.header_px}px" if t.header_px else "") for t in run.tiles))
    print(f"  analysed {len(run.cameras[0].stats)} frames at {run.analysed_fps:g} fps in "
          f"{run.wall_s:.1f} s ({run.duration_s / max(run.wall_s, 1e-9):.0f}x realtime)")

    for cam in run.cameras:
        path, res = written[cam.cfg.sensor]
        s = res["summary"]
        match = res["picture"]["overlay_match"]
        match_txt = "no overlay colour recorded" if match is None else f"line overlay match {match:.0%}"
        print(f"\n  {cam.cfg.sensor}  (picture #{cam.placement.tile.index}, {match_txt}, "
              f"rule: {cam.cfg.rule})")
        for w in res["warnings"]:
            print(f"    WARNING: {w}")
        print(f"    Active:     {s['n_ranges']} ranges, {fmt_hms(s['active_s'])} "
              f"({100 - s['eliminated_pct']:.1f}%)")
        print(f"    Eliminated: {fmt_hms(s['eliminated_s'])} of {fmt_hms(run.duration_s)} "
              f"-> {s['eliminated_pct']:.1f}% of runtime")
        sens = ", ".join(f"{x['min_blob_px']}px: {x['eliminated_pct']:.1f}%"
                         for x in res["diagnostics"]["threshold_sensitivity"])
        print(f"    Min-blob sensitivity (eliminated %): {sens}")
        print(f"    wrote {path}")
        if args.debug:
            for dp in write_debug(run, cam, res, args.video, camera_dir(out_dir, cam.cfg.sensor),
                                  seed=args.seed):
                print(f"    wrote {dp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
