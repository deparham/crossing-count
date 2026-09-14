"""CrossingCount in its own window, not a browser tab.

The pages are the same. They are shown by the computer's own web engine (WebKit on a Mac,
Edge WebView2 on Windows) in a window of the app, with the server running inside the app
on 127.0.0.1 as before, so nothing leaves the computer. Closing the window closes the
app, as Quit on the page does.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import threading
import time
from typing import Any

import uvicorn

from . import paths

TITLE = "CrossingCount"
START_WAIT_S = 120.0  # the first start can take a while (the detector's libraries load)


def available() -> bool:
    """pywebview is there (else the page opens in the browser, as before)."""
    try:
        importlib.import_module("webview")
    except ImportError:
        return False
    return True


def _webview() -> Any:
    return importlib.import_module("webview")


def applescript_text(s: str) -> str:
    """A string as an AppleScript literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


class Api:
    """What the page may ask of its window (window.pywebview.api, in the page)."""

    def __init__(self, base: str) -> None:
        self._base = base.rstrip("/")

    def open(self, path: str) -> None:
        """Another of the app's own pages (the gold set, marking heads), in its own window."""
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            return
        _webview().create_window(TITLE, self._base + path, width=1280, height=860,
                                 min_size=(900, 600), text_select=True)

    def notify(self, title: str, body: str) -> None:
        """A notice from the system: the Mac's Notification Centre (the page shows a banner
        in any case)."""
        if sys.platform == "darwin":
            script = (f"display notification {applescript_text(str(body))} "
                      f"with title {applescript_text(str(title))}")
            subprocess.run(["/usr/bin/osascript", "-e", script], check=False, capture_output=True)


def _dock_icon() -> None:
    """From the project folder the window belongs to Python: give it CrossingCount's icon."""
    if sys.platform != "darwin" or paths.FROZEN:
        return
    try:
        appkit = importlib.import_module("AppKit")
    except ImportError:
        return
    image = appkit.NSImage.alloc().initWithContentsOfFile_(
        str(paths.SOURCE_ROOT / "packaging" / "mac" / "icon-1024.png"))
    if image is not None:
        appkit.NSApplication.sharedApplication().setApplicationIconImage_(image)


def show(url: str, server: uvicorn.Server | None = None) -> int:
    """The app's window on url, until it is closed; then the server (if given) stops."""
    webview = _webview()
    webview.settings["ALLOW_DOWNLOADS"] = True  # the report's download link
    webview.create_window(TITLE, url, js_api=Api(url), width=1400, height=920,
                          min_size=(960, 640), text_select=True)
    _dock_icon()
    # not private: the page's small memories (the last brand chosen) last between runs
    webview.start(private_mode=False, storage_path=str(paths.data_root() / "window"))
    if server is not None:
        server.should_exit = True
    return 0


def run(server: uvicorn.Server, url: str) -> int:
    """Start the server inside the app, then its window; closing the window stops both."""
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + START_WAIT_S
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            print("CrossingCount could not start (is another program using its port?).",
                  flush=True)
            return 1
        time.sleep(0.05)
    return show(url, server)
