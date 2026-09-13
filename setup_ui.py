#!/usr/bin/env python3
"""Camera setup page: draw each camera's counting line and zones in your browser.

    uv run setup_ui.py VIDEO [--port 8765]

Opens a page served from this Mac only (127.0.0.1). Pick a camera picture, draw
the counting line, click the inside side, draw the mask / filter / exclusion
zones, and save: the config is written to sites/<camera>.json.
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

from crossing_count.webapp import create_app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("video", type=Path)
    ap.add_argument("--sites", type=Path, default=Path("sites"), help="config folder (default sites/)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't open the page automatically")
    args = ap.parse_args(argv)
    if not args.video.is_file():
        ap.error(f"video not found: {args.video}")
    args.sites.mkdir(exist_ok=True)

    print("reading video (people-free picture) ...", flush=True)
    app = create_app(args.video, args.sites)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Setup page: {url}   (local only; press Ctrl+C here to stop)", flush=True)
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
