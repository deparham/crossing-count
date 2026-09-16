#!/usr/bin/env python3
"""Count wizard: pick the footage, draw, count, check the crossings, get a PowerPoint.

    uv run wizard.py [--port 8780]

Opens a page served from this computer only (127.0.0.1); with --network (or sharing
switched on in the app), people on this network can use it too, with an access code
(crossing_count/network.py). It asks for the footage,
lets you draw each camera, asks Traffic In or Out and RetailNext's number,
counts automatically while showing what it is doing, then plays each crossing
it found for a quick yes/no before writing the report. The logo comes from
assets/logo.png (or --logo).
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from crossing_count import paths, window
from crossing_count.app import PORT, serving
from crossing_count.network import NETWORK_PORT
from crossing_count.wizard_app import create_wizard_app


def _open_when_up(server: uvicorn.Server, url: str) -> None:
    """Open the page once the server answers: the app's first start can take a while."""
    while not server.started:
        if server.should_exit:
            return
        time.sleep(0.1)
    webbrowser.open(url)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--logo", type=Path, help="logo image for the report (default assets/logo.*)")
    ap.add_argument("--sites", type=Path, help="camera drawings folder (default sites/)")
    ap.add_argument("--runs-root", type=Path, help="folder holding runs/ (default: the data folder)")
    ap.add_argument("--browser", action="store_true",
                    help="open it in the browser instead of its own window")
    ap.add_argument("--no-browser", action="store_true",
                    help="only the server: no window and no browser tab")
    ap.add_argument("--network", action="store_true",
                    help="also let people on this network use it, each on their own validation,\n"
                         "with the access code shown on the page (it stays on until switched off)")
    ap.add_argument("--network-port", type=int, default=NETWORK_PORT,
                    help=f"port for the network (default {NETWORK_PORT})")
    args = ap.parse_args(argv)
    paths.prepare_data_root()
    url = f"http://127.0.0.1:{args.port}/"
    show = "none" if args.no_browser else "browser" if args.browser else "window"
    if show == "window" and not window.available():
        show = "browser"
    if serving(args.port):  # opened again while it runs: show it instead of failing
        print(f"Crossing Count is already running at {url}", flush=True)
        if show == "window":
            return window.show(url)
        if show == "browser":
            webbrowser.open(url)
        return 0
    print(f"Crossing Count is running at {url}\n"
          f"Your data: {paths.data_root()}\n"
          f"Quit on the page, or close its window, to stop it.", flush=True)
    app = create_wizard_app(args.sites or paths.sites_dir(), args.runs_root or paths.data_root(),
                            logo=args.logo or paths.logo_path(), network_port=args.network_port)
    if args.network or paths.load_settings().get("network"):  # on until switched off on the page
        sharing = app.state.sharing
        if sharing.start():
            paths.save_settings({"network": True})
            print(f"Shared on this network: {', '.join(sharing.urls)}\n"
                  f"Access code: {sharing.access.code}", flush=True)
        else:
            print(f"Not shared on this network: {sharing.error}", flush=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port,
                                           log_level="warning"))
    if show == "window":
        return window.run(server, url)
    if show == "browser":
        threading.Thread(target=_open_when_up, args=(server, url), daemon=True).start()
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
