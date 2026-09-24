"""Where a finished validation is delivered: the brand's own folder on the shared drive.

    <delivery>/<Brand>/<Brand Store - CODE>/
        the report, as a PDF and a PowerPoint
        the footage the count was made from

The customer gets the report, not the workings: the validation's own record stays with the
validation, in validations/<ID>/ on this computer.

The brand comes from the store code's prefix, because the store's name rarely carries it
(CN-123 is "Tweed Heads"). A brand folder already on the drive is used exactly as it is
spelled there - "Johnny Big", "TAROCASH" - so a second folder is never made beside one that
means the same thing. Matching ignores case, spaces and punctuation; nothing else.

Nothing is delivered without being asked for. The footage shows identifiable people and
belongs to the retailer, so copying it to a shared drive puts it somewhere new: that is the
person's decision each time, and docs/PRIVACY.md says what it means.
"""

from __future__ import annotations

import functools
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import graph, paths

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


def destination(root: Path, store: dict[str, Any]) -> tuple[Path, str, bool]:
    """Where this validation belongs, its brand, and whether the brand's folder is there."""
    code = str(store.get("code") or "").strip()
    if not code:
        raise DeliveryError("The store code is empty, so there is no brand to file this under.")
    brand = brand_of(code)
    if brand is None:
        raise DeliveryError(
            f"{code} does not say which brand it is. Add its prefix on the Report page "
            f"(Shared folder), or file this validation by hand.")
    folder = brand_folder(root, brand)
    return folder / folder_name(brand, str(store.get("name") or ""), code), brand, folder.is_dir()


def where(root: Path, store: dict[str, Any]) -> Path:
    """The folder this validation belongs in, made or not."""
    return destination(root, store)[0]


def _folders(root: Path) -> list[str]:
    try:
        return sorted(p.name for p in root.iterdir() if p.is_dir())
    except OSError:
        return []


def plan(root: Path, store: dict[str, Any], report: Path,
         video: Path | None = None) -> dict[str, Any]:
    """What would be copied where, for the person to agree to before anything is.

    Including a word of warning when the brand has no folder here but other brands do. The
    likeliest reason is that the folder chosen is a level out - the one holding the brands'
    parent rather than the brands - and a brand folder made in the wrong place on a shared
    drive is work nobody finds again.
    """
    out, brand, brand_there = destination(root, store)
    files: list[dict[str, Any]] = []
    for p in _files(report, video):
        try:
            size = p.stat().st_size
        except OSError:
            continue
        files.append({"name": p.name, "bytes": size, "footage": p == video})
    here = _folders(root)
    warn = None
    if not brand_there and here:
        warn = (f"There is no {brand} folder in the folder you chose, which holds "
                f"{', '.join(here[:4])}{' and more' if len(here) > 4 else ''}. A {brand} folder "
                f"will be made here. If the brands live somewhere else, change the folder "
                f"first.")
    return {"folder": str(out), "exists": out.is_dir(), "files": files, "brand": brand,
            "brand_exists": brand_there, "warn": warn,
            "bytes": sum(int(f["bytes"]) for f in files)}


def _files(report: Path, video: Path | None) -> list[Path]:
    out = [p for p in (report, report.with_suffix(".pdf")) if p.is_file()]
    if video is not None and video.is_file():
        out.append(video)
    return list(dict.fromkeys(out))


def deliver(root: Path, store: dict[str, Any], report: Path,
            video: Path | None = None) -> dict[str, Any]:
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
    for src in _files(report, video):
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


# ---------- straight to SharePoint, rather than into the folder OneDrive carries up ----------
# The same folder and the same files, put there by the app itself. A 79 MB export copied into
# the synced folder is only delivered once OneDrive has finished with it, and until then the
# customer's folder is half there; here the upload is the delivery, and it either worked or
# it said why.

def _telling(on: Callable[[str, int, int], None], name: str, sent: int, size: int) -> None:
    on(name, sent, size)


def online_where(root: Path, store: dict[str, Any], host: str) -> graph.Place | None:
    """The brand's folder on SharePoint, or None when nothing says where that is."""
    return graph.place_for(where(root, store), host)


def online_plan(place: graph.Place, report: Path, video: Path | None = None) -> dict[str, Any]:
    """What would be uploaded where, for the person to agree to before any of it is."""
    files: list[dict[str, Any]] = []
    for p in _files(report, video):
        try:
            files.append({"name": p.name, "bytes": p.stat().st_size, "footage": p == video})
        except OSError:
            continue
    return {"folder": place.path, "site": place.site, "online": True, "files": files,
            "bytes": sum(int(f["bytes"]) for f in files)}


def online_deliver(place: graph.Place, report: Path, video: Path | None = None,
                   on: Callable[[str, int, int], None] | None = None) -> dict[str, Any]:
    """Upload this validation into the brand's folder on SharePoint.

    The folder, and every folder above it, is made first. A file already there is not
    replaced: SharePoint is asked to rename the new one instead, because a delivered folder
    is the customer's (graph.upload).
    """
    drive = graph.drive_of(place)
    try:
        graph.folder(drive, place.path)
        done: list[str] = []
        for src in _files(report, video):
            watch: Callable[[int, int], None] | None = None
            if on is not None:
                watch = functools.partial(_telling, on, src.name)
            got = graph.upload(drive, place.under(src.name).path, src, on=watch)
            done.append(str(got.get("name") or src.name))
    except graph.GraphError as exc:
        raise DeliveryError(str(exc)) from exc
    return {"folder": place.path, "site": place.site, "files": done, "online": True,
            "footage": bool(video is not None and video.is_file()),
            "url": f"{place.site}/{place.library.replace(' ', '%20')}/"
                   + "/".join(quote(p) for p in place.path.split("/"))}
