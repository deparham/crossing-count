"""Where a result came from: the software, the detector and its weights, the settings, and
the computer. Recorded in every finalised validation's manifest (runs.py), so a result can
be reproduced and a different setup is never mistaken for the same one.
"""

from __future__ import annotations

import functools
import hashlib
import importlib
import importlib.metadata
import os
import platform
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import paths
from .version import app_version

PACKAGES = ("torch", "ultralytics", "opencv-python", "av", "numpy", "fastapi", "python-pptx",
            "pywebview")


@functools.lru_cache(maxsize=64)
def _sha(path: str, size: int, mtime: float) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def file_sha256(p: Path) -> str:
    st = p.stat()
    return _sha(str(p), st.st_size, st.st_mtime)


def software() -> dict[str, Any]:
    versions = {}
    for name in PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return {"crossingcount": app_version(), "python": sys.version.split()[0], "packages": versions}


def hardware() -> dict[str, Any]:
    out: dict[str, Any] = {"os": platform.platform(), "machine": platform.machine(),
                           "processor": platform.processor() or None, "cpus": os.cpu_count()}
    try:
        psutil = importlib.import_module("psutil")
        out["memory_gb"] = round(psutil.virtual_memory().total / 2**30, 1)
    except ImportError:
        pass
    try:
        torch = importlib.import_module("torch")
        out["cuda"] = bool(torch.cuda.is_available())
        mps = getattr(torch.backends, "mps", None)
        out["mps"] = bool(mps and mps.is_available())
        if out["cuda"]:
            out["gpu"] = torch.cuda.get_device_name(0)
    except ImportError:
        pass
    return out


def _read(p: Path) -> dict[str, Any]:
    import json

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def detection(run_dir: Path, cameras: Iterable[str]) -> list[dict[str, Any]]:
    """Each camera's automatic count as it ran: the detector and its settings, the weights'
    SHA-256, the tracker and counting-rule settings, and the drawing used."""
    from .gating import camera_dir

    out = []
    for cam in cameras:
        cand = _read(camera_dir(run_dir, cam) / "candidates.json")
        if not cand:
            continue
        det = cand.get("detector") or {}
        name = str(det.get("model") or "")
        weights = next((d / name for d in paths.models_dirs() if name and (d / name).is_file()),
                       None)
        out.append({"camera": cam, "detector": det,
                    "weights_sha256": file_sha256(weights) if weights else None,
                    "rule": cand.get("rule_params"), "config": cand.get("config"),
                    "tool_version": cand.get("tool_version"),
                    "processed_duration_s": cand.get("processed_duration_s")})
    return out
