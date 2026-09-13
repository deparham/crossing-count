#!/usr/bin/env python3
"""M3 - review page: turn proposals into verified counts.

    uv run review.py VIDEO [--operator "Your Name"] [--sample 120] [--port 8770]

Opens a page served from this Mac only (127.0.0.1). It works through, per
camera, every proposed crossing, a sample of the rule's rejections (all lost
entries and duplicates), and every stretch of unexplained motion. Decisions are
saved on every key press to runs/<video>/review/decisions.json; close the page
any time and it resumes where you stopped. Then run export.py.
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

from crossing_count.review import ReviewError
from crossing_count.review_app import create_review_app
from crossing_count.util import default_run_dir


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("video", type=Path)
    ap.add_argument("--operator", default="", help="reviewer's name (also editable on the page)")
    ap.add_argument("--sample", type=float, help="as given to gate.py / detect.py")
    ap.add_argument("--out-dir", type=Path, help="run directory (default: as detect.py)")
    ap.add_argument("--discard-sample", type=float, default=0.25,
                    help="share of rule rejections to audit, besides every lost entry and "
                         "duplicate (default 0.25)")
    ap.add_argument("--seed", type=int, default=0, help="seed for the rejection sample")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    if not args.video.is_file():
        ap.error(f"video not found: {args.video}")
    run_dir = args.out_dir or default_run_dir(args.video, args.sample)
    try:
        app = create_review_app(args.video, run_dir, args.operator, args.seed, args.discard_sample)
    except ReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Review page: {url}   (local only; press Ctrl+C here to stop)", flush=True)
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
