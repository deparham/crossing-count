#!/usr/bin/env python3
"""Proposed in / out counts per camera and in total, with clock times.

    uv run proposed.py VIDEO [--sample 120] [--start "2026-09-12 11:30:00"]

Reads detect.py's output for VIDEO. The clock time of video 00:00:00 comes from
the export filename ("... 2026-09-12-113000 AEST to ...") unless --start is
given. These are proposals, not verified counts: check the rejected tracks and
unexplained stretches it lists as well.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from crossing_count.summary import format_summary, summarise
from crossing_count.util import default_run_dir
from crossing_count.video import parse_filename_interval


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("video", type=Path)
    ap.add_argument("--sample", type=float, help="as given to gate.py / detect.py")
    ap.add_argument("--out-dir", type=Path, help="run directory (default: as detect.py)")
    ap.add_argument("--start", help='wall-clock time of video 00:00:00, e.g. "2026-09-12 11:30:00"')
    args = ap.parse_args(argv)

    run_dir = args.out_dir or default_run_dir(args.video, args.sample)
    if not run_dir.is_dir():
        print(f"error: {run_dir} not found; run gate.py and detect.py on this video first",
              file=sys.stderr)
        return 2
    start: datetime | None = None
    if args.start:
        start = datetime.fromisoformat(args.start)
    else:
        interval = parse_filename_interval(args.video.name)
        if interval is not None:
            start = datetime.fromisoformat(interval.start)
    summary = summarise(run_dir, start)
    if not summary["cameras"]:
        print(f"error: no detect.py results in {run_dir}", file=sys.stderr)
        return 2
    print("\n".join(format_summary(summary, args.video.name, start)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
