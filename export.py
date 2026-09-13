#!/usr/bin/env python3
"""M4 - export verified counts, pipeline metrics and a report.

    uv run export.py VIDEO [--operator NAME] [--sensor-in 20 --sensor-out 23]
                           [--sensor CN-123-PB1:in=12,out=14 ...] [--start "2026-09-12 11:30:00"]

Reads the review decisions (review.py) and writes, to runs/<video>/export/:
  <camera>.csv            the manual tool's layout: header block, then
                          video_time,video_seconds,clock_time,direction,tags
  pipeline_metrics.json   how often the pipeline was right, measured by the review
  report.html             verified in/out, per camera, per 15 minutes, sensor accuracy
--sensor-in/--sensor-out are the sensor's combined numbers for the same period.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from crossing_count.export import SensorCounts, build_export, parse_sensor_arg, write_export
from crossing_count.review import ReviewError, ReviewSession
from crossing_count.util import default_run_dir
from crossing_count.video import parse_filename_interval


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("video", type=Path)
    ap.add_argument("--operator", help="reviewer's name (default: as saved by review.py)")
    ap.add_argument("--start", help='clock time of video 00:00:00 (default: from the filename)')
    ap.add_argument("--sensor-in", type=int, help="sensor's combined IN count for the period")
    ap.add_argument("--sensor-out", type=int, help="sensor's combined OUT count for the period")
    ap.add_argument("--sensor", action="append", default=[], metavar="CAMERA:in=N,out=M",
                    help="sensor counts for one camera (repeatable)")
    ap.add_argument("--sample", type=float, help="as given to gate.py / detect.py")
    ap.add_argument("--out-dir", type=Path, help="run directory (default: as detect.py)")
    args = ap.parse_args(argv)

    run_dir = args.out_dir or default_run_dir(args.video, args.sample)
    try:
        session = ReviewSession(run_dir, args.operator or "")
    except ReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.operator:
        session.set_operator(args.operator)

    start: datetime | None = None
    if args.start:
        start = datetime.fromisoformat(args.start)
    else:
        interval = parse_filename_interval(args.video.name)
        if interval is not None:
            start = datetime.fromisoformat(interval.start)

    sensor = SensorCounts()
    try:
        for value in args.sensor:
            camera, counts = parse_sensor_arg(value)
            if camera is None:
                sensor.total = counts
            else:
                sensor.per_camera[camera] = counts
    except ValueError as exc:
        ap.error(str(exc))
    if args.sensor_in is not None or args.sensor_out is not None:
        sensor.total = {k: v for k, v in (("in", args.sensor_in), ("out", args.sensor_out))
                        if v is not None}

    export = build_export(session, start, None if sensor.is_empty() else sensor)
    paths = write_export(export, run_dir / "export")

    for w in export["warnings"]:
        print(f"WARNING: {w}")
    print(f"\nVerified counts (review {export['review']['done']}/{export['review']['total']})")
    for c in export["cameras"]:
        print(f"  {c['sensor']:<16} in {c['in']:>4}   out {c['out']:>4}")
    print(f"  {'All cameras':<16} in {export['total']['in']:>4}   out {export['total']['out']:>4}")
    if export["total_comparison"]:
        for d, x in export["total_comparison"].items():
            if x["sensor"] is not None:
                acc = "–" if x["accuracy_pct"] is None else f"{x['accuracy_pct']}%"
                print(f"  sensor {d.upper()}: {x['sensor']} vs verified {x['verified']} "
                      f"-> accuracy {acc}")
    for p in paths:
        print(f"  wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
