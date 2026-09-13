"""Where the program's files live.

Run from the source folder, everything is in the project: models/, sites/, runs/.
Installed as an app, the program folder is read-only, so the user's own data
(camera drawings, runs, settings) goes to a per-user folder, and files shipped
with the program (model weights, logo, default drawings) are read from the
bundle. The environment variable CROSSING_COUNT_HOME overrides the data folder.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

FROZEN = bool(getattr(sys, "frozen", False))
SOURCE_ROOT = Path(__file__).resolve().parents[2]
SETTINGS = "settings.json"


def bundle_root() -> Path:
    """Files shipped with the program: models/, assets/, sites/ (defaults)."""
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return SOURCE_ROOT


def data_root() -> Path:
    """The user's own files: sites/, runs/, settings.json."""
    env = os.environ.get("CROSSING_COUNT_HOME")
    if env:
        return Path(env).expanduser()
    if not FROZEN:
        return SOURCE_ROOT
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "CrossingCount"


def sites_dir() -> Path:
    return data_root() / "sites"


def models_dirs() -> list[Path]:
    """Where model weights are looked for: the user's folder first, then the program's."""
    dirs: list[Path] = []
    for d in (data_root() / "models", bundle_root() / "models"):
        if d not in dirs:
            dirs.append(d)
    return dirs


def logo_path() -> Path | None:
    for root in (data_root(), bundle_root()):
        found = sorted((root / "assets").glob("logo.*"))
        if found:
            return found[0]
    return None


def prepare_data_root() -> Path:
    """Create the data folders; an installed copy starts with the drawings shipped with it."""
    root = data_root()
    for sub in ("sites", "runs"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    shipped = bundle_root() / "sites"
    if shipped.is_dir() and shipped.resolve() != (root / "sites").resolve():
        for p in shipped.glob("*.json"):
            dest = root / "sites" / p.name
            if not dest.exists():
                shutil.copy2(p, dest)
    return root


def load_settings() -> dict[str, Any]:
    try:
        data = json.loads((data_root() / SETTINGS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_settings(values: dict[str, Any]) -> dict[str, Any]:
    from .util import write_json_atomic

    settings = {**load_settings(), **values}
    write_json_atomic(data_root() / SETTINGS, settings)
    return settings
