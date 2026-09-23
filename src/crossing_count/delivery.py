"""Where a finished validation is delivered: the brand's own folder on the shared drive.

    <delivery>/<Brand>/<Brand Store - CODE>/
        the report, as a PDF and a PowerPoint
        the footage the count was made from
        result.json and manifest.json, so the folder says what it is

The brand comes from the store code's prefix, because the store's name rarely carries it
(CN-123 is "Tweed Heads"). A brand folder already on the drive is used exactly as it is
spelled there - "Johnny Big", "TAROCASH" - so a second folder is never made beside one that
means the same thing. Matching ignores case, spaces and punctuation; nothing else.

Nothing is delivered without being asked for. The footage shows identifiable people and
belongs to the retailer, so copying it to a shared drive puts it somewhere new: that is the
person's decision each time, and docs/PRIVACY.md says what it means.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from . import paths

# A store code's prefix, and the brand it belongs to. The folders on the drive are spelled
# as their owners spell them; what is here is only used when no folder matches yet.
BRANDS: dict[str, str] = {
    "CN": "Connor",
    "JB": "Johnny Big",
    "YD": "YD",
    "TC": "TAROCASH",
    "RW": "Rockwear",
}
PREFIX = re.compile(r"^([A-Za-z]{2,4})[-_ ]?\d")
NOT_IN_NAMES = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')  # what a file system will not take


class DeliveryError(Exception):
    """A validation that cannot be delivered, said plainly."""


def _same(a: str, b: str) -> bool:
    """Two folder names meaning the same brand: case, spaces and punctuation aside."""
    squash = re.compile(r"[^a-z0-9]+")
    return squash.sub("", a.lower()) == squash.sub("", b.lower())


def brands() -> dict[str, str]:
    """The prefixes known here, with anything the person added in the settings."""
    extra = paths.load_settings().get("brands") or {}
    return {**BRANDS, **{str(k).upper(): str(v) for k, v in dict(extra).items()}}


def brand_of(code: str) -> str | None:
    """"Connor" from "CN-123". None when the code says nothing about the brand."""
    m = PREFIX.match(str(code).strip())
    return brands().get(m.group(1).upper()) if m else None


def brand_folder(root: Path, brand: str) -> Path:
    """The brand's folder on the drive: the one already there, however it is spelled."""
    try:
        for p in sorted(root.iterdir()):
            if p.is_dir() and _same(p.name, brand):
                return p
    except OSError:  # not reachable (a drive not mounted, no permission): say so later
        pass
    return root / brand


def folder_name(brand: str, store: str, code: str) -> str:
    """"Connor Tweed Heads - CN-123": the brand, where the store is, and its code.

    Read by people on a shared drive, so it keeps its capitals and spaces; only what a file
    system will not take is replaced.
    """
    words = " ".join(w for w in (brand, str(store).strip()) if w)
    return NOT_IN_NAMES.sub("-", f"{words} - {str(code).strip()}").strip(" .")


def where(root: Path, store: dict[str, Any]) -> Path:
    """The folder this validation belongs in, made or not."""
    code = str(store.get("code") or "").strip()
    if not code:
        raise DeliveryError("The store code is empty, so there is no brand to file this under.")
    brand = brand_of(code)
    if brand is None:
        raise DeliveryError(
            f"{code} does not say which brand it is. Add its prefix on the Report page "
            f"(Shared folder), or file this validation by hand.")
    return brand_folder(root, brand) / folder_name(brand, str(store.get("name") or ""), code)


def plan(root: Path, store: dict[str, Any], report: Path, video: Path | None,
         run_folder: Path | None) -> dict[str, Any]:
    """What would be copied where, for the person to agree to before anything is."""
    out = where(root, store)
    files: list[dict[str, Any]] = []
    for p in _files(report, video, run_folder):
        try:
            size = p.stat().st_size
        except OSError:
            continue
        files.append({"name": p.name, "bytes": size, "footage": p == video})
    return {"folder": str(out), "exists": out.is_dir(), "files": files,
            "bytes": sum(int(f["bytes"]) for f in files)}


def _files(report: Path, video: Path | None, run_folder: Path | None) -> list[Path]:
    out = [p for p in (report, report.with_suffix(".pdf")) if p.is_file()]
    for name in ("result.json", "manifest.json"):
        if run_folder is not None and (run_folder / name).is_file():
            out.append(run_folder / name)
    if video is not None and video.is_file():
        out.append(video)
    return list(dict.fromkeys(out))


def deliver(root: Path, store: dict[str, Any], report: Path, video: Path | None = None,
            run_folder: Path | None = None) -> dict[str, Any]:
    """Copy this validation into the brand's folder. The footage only when it is given.

    A file already there is written beside the new one rather than over it: a delivered
    folder is someone else's, and nothing of theirs is replaced.
    """
    out = where(root, store)
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DeliveryError(f"{out} could not be made ({exc}). Is the drive connected?") from exc
    done: list[str] = []
    for src in _files(report, video, run_folder):
        dst = out / src.name
        k = 2
        while dst.exists():
            dst = out / f"{src.stem} ({k}){src.suffix}"
            k += 1
        try:
            shutil.copy2(src, dst)
        except OSError as exc:
            raise DeliveryError(f"{src.name} could not be copied to {out} ({exc}).") from exc
        done.append(dst.name)
    return {"folder": str(out), "files": done,
            "footage": bool(video is not None and video.is_file())}
