"""The installed program: one executable for the count wizard and every pipeline step.

    CrossingCount                   opens the count wizard in the browser
    CrossingCount gate VIDEO ...    the same as `uv run gate.py VIDEO ...`
    CrossingCount --self-test       checks the bundle loads everything, then exits

The wizard runs gate and detect as child processes of this same executable.
"""

from __future__ import annotations

import importlib
import multiprocessing
import socket
import subprocess
import sys
import webbrowser
from typing import Any

from .paths import FROZEN, SOURCE_ROOT, data_root, models_dirs, prepare_data_root

PORT = 8780
COMMANDS = {
    "gate": "gate", "detect": "detect", "count": "count", "review": "review",
    "export": "export", "setup": "setup_ui", "proposed": "proposed", "trace": "trace_line",
    "wizard": "wizard", "label": "label", "train-heads": "train_heads",
    "retailnext": "retailnext",
}


def script(name: str) -> Any:
    """One of the project's command-line scripts, as a module."""
    if not FROZEN and str(SOURCE_ROOT) not in sys.path:
        sys.path.insert(0, str(SOURCE_ROOT))
    return importlib.import_module(name)


def serving(port: int = PORT) -> bool:
    """True when a program on this computer already listens on the port: CrossingCount, opened before."""
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def open_mac_app() -> int:
    """The Mac app: show the page if CrossingCount already runs, else start it in the background.

    The app itself ends at once, so opening it again from its icon always brings the page
    back (a Mac app that stays open is only brought to the front, and this one has no
    window). Quit on the page stops it.
    """
    if serving():
        webbrowser.open(f"http://127.0.0.1:{PORT}/")
        return 0
    log = data_root() / "logs" / "wizard.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as out:
        subprocess.Popen([sys.executable, "wizard"], stdin=subprocess.DEVNULL, stdout=out,
                         stderr=subprocess.STDOUT, start_new_session=True)
    return 0


def self_test() -> int:
    import av
    import cv2
    import pptx
    import torch
    import ultralytics

    from .detector import load_model
    from .wizard_app import create_wizard_app

    for name in set(COMMANDS.values()):
        script(name)
    weights = sorted(p.name for d in models_dirs() if d.is_dir() for p in d.glob("*.pt"))
    if not weights:
        print(f"self-test FAILED: no model weights in {[str(d) for d in models_dirs()]}")
        return 1
    for name in weights:
        load_model(name)
    create_wizard_app()
    print(f"self-test ok: torch {torch.__version__}, ultralytics {ultralytics.__version__}, "
          f"opencv {cv2.__version__}, av {av.__version__}, python-pptx {pptx.__version__}, "
          f"weights {weights}")
    return 0


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    args = sys.argv[1:] if argv is None else argv
    prepare_data_root()
    if args and args[0] == "--self-test":
        return self_test()
    if args and args[0] in COMMANDS:
        return int(script(COMMANDS[args[0]]).main(args[1:]) or 0)
    if FROZEN and sys.platform == "darwin" and not args:
        return open_mac_app()
    return int(script("wizard").main(args) or 0)
