#!/usr/bin/env python3
"""Count wizard: pick the footage, draw, count, check the crossings, get a PowerPoint.

    uv run wizard.py [--port 8780]

Opens a page served from this computer only (127.0.0.1). It asks for the footage,
lets you draw each camera, asks Traffic In or Out and RetailNext's number,
counts automatically while showing what it is doing, then plays each crossing
it found for a quick yes/no before writing the report. The logo comes from
assets/logo.png (or --logo).
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

from crossing_count import paths
from crossing_count.wizard_app import create_wizard_app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--logo", type=Path, help="logo image for the report (default assets/logo.*)")
    ap.add_argument("--sites", type=Path, help="camera drawings folder (default sites/)")
    ap.add_argument("--runs-root", type=Path, help="folder holding runs/ (default: the data folder)")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    paths.prepare_data_root()
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Crossing Count is running at {url}\n"
          f"Your data: {paths.data_root()}\n"
          f"Keep this window open while you work; close it to quit.", flush=True)
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app = create_wizard_app(args.sites or paths.sites_dir(), args.runs_root or paths.data_root(),
                            logo=args.logo or paths.logo_path())
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
