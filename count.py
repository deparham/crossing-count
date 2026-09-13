#!/usr/bin/env python3
"""Manual count: watch each camera and press I or O at every crossing.

    uv run count.py VIDEO [--operator "Your Name"] [--port 8772]
    uv run count.py VIDEO --export        # write the CSV files and report, no page

No detection, no tracking: a person does all the counting. The page plays one
camera at a time, records the time of every key press, shows which stretches
you have watched, and compares your counts with the sensor's numbers for each
15-minute interval (type them into the page). Everything is saved on every key
press to runs/<video>/manual/counts.json; close the page any time and it resumes.
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

import uvicorn

from crossing_count.manual import ManualError, ManualSession
from crossing_count.manual_app import create_manual_app
from crossing_count.util import default_run_dir


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("video", type=Path)
    ap.add_argument("--operator", default="", help="your name (also editable on the page)")
    ap.add_argument("--start", help='clock time at video 00:00:00, e.g. "2026-09-12 11:30:00" '
                                    "(default: from the filename)")
    ap.add_argument("--cameras", help='camera names, left to right, e.g. "CN-123-PB1,CN-123-R2" '
                                      "(default: from gate.py, else Camera 1, 2, ...)")
    ap.add_argument("--out-dir", type=Path, help="run directory (default: runs/<video>)")
    ap.add_argument("--export", action="store_true",
                    help="write the CSV files and report.html, then exit")
    ap.add_argument("--port", type=int, default=8772)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    if not args.video.is_file():
        ap.error(f"video not found: {args.video}")
    try:
        start = datetime.fromisoformat(args.start) if args.start else None
    except ValueError:
        ap.error(f"--start must look like 2026-09-12 11:30:00, not {args.start!r}")
    names = [n.strip() for n in args.cameras.split(",")] if args.cameras else None
    run_dir = args.out_dir or default_run_dir(args.video, None)
    try:
        session = ManualSession(args.video, run_dir, args.operator, start, names)
    except ManualError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.export:
        for p in session.export():
            print(f"wrote {p}")
        s = session.summary()
        for c in s["cameras"]:
            print(f"  {c['name']:<16} in {c['in']:>4}   out {c['out']:>4}   "
                  f"watched {c['watched_pct']}%")
        for w in s["warnings"]:
            print(f"WARNING: {w}")
        return 0
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Count page: {url}   (local only; press Ctrl+C here to stop)", flush=True)
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_manual_app(session), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
