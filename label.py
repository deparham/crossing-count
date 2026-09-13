#!/usr/bin/env python3
"""Mark heads on camera pictures, to train a detector for overhead views.

    uv run label.py [--port 8790]

Opens a page served from this computer only (127.0.0.1). Add frames from your
videos; each head is already marked from the current detector's people where a
video was counted with the wizard, so you mostly correct. Then run
train_heads.py. The same page is in the count wizard under "Mark heads".
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

import uvicorn

from crossing_count import paths
from crossing_count.heads_app import create_label_app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    paths.prepare_data_root()
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Head marking page: {url}   (local only; press Ctrl+C here to stop)", flush=True)
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_label_app(), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
