"""Contact sheets for checking M2 by eye. Images are derived from CCTV: they are
written under runs/ (gitignored) and stay on this machine."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

from . import render
from . import video as vid
from .config import SiteConfig, bind
from .layout import Tile
from .util import fmt_hms

Image = NDArray[np.uint8]
IN_COLOR = (0, 200, 0)
OUT_COLOR = (0, 0, 230)
DISCARD_COLOR = (0, 165, 255)


def _draw_path(img: Image, path: list[list[float]], t: float, color: tuple[int, int, int]) -> None:
    if len(path) >= 2:
        pts = np.array([[p[1], p[2]] for p in path], dtype=np.int32)
        cv2.polylines(img, [pts], False, color, 1, cv2.LINE_AA)
    if path:
        now = min(path, key=lambda p: abs(p[0] - t))
        cv2.circle(img, (int(now[1]), int(now[2])), 5, color, 2, cv2.LINE_AA)


def _strip(frames: list[vid.Frame], tile: Tile, draw: Any, text: str) -> Image:
    tiles = []
    for f in frames:
        img = draw(f.image.copy(), f.t)
        tiles.append(render.label(tile.crop(img), f"{fmt_hms(f.t)}  {text}"))
    return np.hstack(tiles) if tiles else np.zeros((10, 10, 3), dtype=np.uint8)


def _sheet(rows: list[Image], path: Path, scale: float = 0.5) -> Path | None:
    if not rows:
        return None
    width = max(r.shape[1] for r in rows)
    padded = [np.pad(r, ((0, 0), (0, width - r.shape[1]), (0, 0))) for r in rows]
    img = cv2.resize(np.vstack(padded), None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return path


def _track_color(track_id: int) -> tuple[int, int, int]:
    hue = (track_id * 47) % 180
    bgr = cv2.cvtColor(np.array([[[hue, 230, 255]]], dtype=np.uint8), cv2.COLOR_HSV2BGR)[0, 0]
    return (int(bgr[0]), int(bgr[1]), int(bgr[2]))


def write_tracks_image(video_path: str | Path, cfg: SiteConfig, tracks_json: dict[str, Any],
                       out_path: Path) -> Path:
    """Every track's path over the people-free median frame: where people actually walk."""
    p = tracks_json["picture"]
    tile = Tile(p["index"], p["x0"], p["y0"], p["x1"], p["y1"], p["header_px"])
    geom = bind(cfg, tile.width, tile.height, tile.origin)
    median = vid.median_of_video(video_path, float(tracks_json["processed_duration_s"]))
    img = render.draw_geometry(median, geom)
    for t in tracks_json["tracks"]:
        pts = np.array([[s[1], s[2]] for s in t["samples"]], dtype=np.int32)
        color = _track_color(t["track_id"])
        if len(pts) >= 2:
            cv2.polylines(img, [pts], False, color, 1, cv2.LINE_AA)
        cv2.circle(img, (int(pts[0][0]), int(pts[0][1])), 3, color, -1)
        cv2.drawMarker(img, (int(pts[-1][0]), int(pts[-1][1])), color, cv2.MARKER_TILTED_CROSS, 7, 1)
    out = render.label(tile.crop(img), f"{len(tracks_json['tracks'])} tracks (dot = start, x = end)")
    big = cv2.resize(out, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_LINEAR)
    cv2.imwrite(str(out_path), big)
    return out_path


def write_detect_debug(video_path: str | Path, cfg: SiteConfig, cand: dict[str, Any],
                       disc: dict[str, Any], unex: dict[str, Any], out_dir: Path,
                       max_items: int = 40, tracks_json: dict[str, Any] | None = None) -> list[Path]:
    """Sheets: candidates (before / at / after the line), discards, unexplained, all tracks."""
    p = cand["picture"]
    tile = Tile(p["index"], p["x0"], p["y0"], p["x1"], p["y1"], p["header_px"])
    geom = bind(cfg, tile.width, tile.height, tile.origin)
    dbg = out_dir / "debug"
    dbg.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if tracks_json is not None:
        written.append(write_tracks_image(video_path, cfg, tracks_json, dbg / "tracks.png"))

    rows: list[Image] = []
    for c in cand["candidates"][:max_items]:
        t = c["t_seconds"]
        times = [max(c["clip_start"], t - 1.0), t, min(c["clip_end"], t + 1.0)]
        color = IN_COLOR if c["direction"] == "in" else OUT_COLOR

        def draw(img: Image, ft: float, c: dict[str, Any] = c, color: tuple[int, int, int] = color) -> Image:
            img = render.draw_geometry(img, geom)
            _draw_path(img, c["path"], ft, color)
            cv2.drawMarker(img, (int(c["crossing_xy"][0]), int(c["crossing_xy"][1])), color,
                           cv2.MARKER_TILTED_CROSS, 10, 2)
            return img

        flags = (" " + ",".join(c["flags"])) if c["flags"] else ""
        rows.append(_strip(vid.grab_frames_at(video_path, times), tile, draw,
                           f"{c['id']} {c['direction'].upper()} conf {c['confidence']:.2f}{flags}"))
    if (sheet := _sheet(rows, dbg / "candidates.jpg")) is not None:
        written.append(sheet)

    rows = []
    for d in disc["discarded"][:max_items]:
        t0, t1 = d["crossings"][0]["t"], d["crossings"][-1]["t"]
        times = sorted({max(d["clip_start"], t0 - 1.0), t0, min(d["clip_end"], max(t1, t0 + 1.5))})

        def draw_d(img: Image, ft: float, d: dict[str, Any] = d) -> Image:
            img = render.draw_geometry(img, geom)
            _draw_path(img, d["path"], ft, DISCARD_COLOR)
            return img

        rows.append(_strip(vid.grab_frames_at(video_path, times), tile, draw_d,
                           f"{d['id']} {d['reason']}" + (f" ({d['pattern']})" if d["pattern"] else "")))
    if (sheet := _sheet(rows, dbg / "discarded.jpg")) is not None:
        written.append(sheet)

    rows = []
    for u in unex["unexplained"][:max_items]:
        s, e = u["start_s"], u["end_s"]
        times = [s + (e - s) * f for f in (0.2, 0.5, 0.8)]

        def draw_u(img: Image, ft: float) -> Image:
            return render.draw_geometry(img, geom)

        rows.append(_strip(vid.grab_frames_at(video_path, times), tile, draw_u,
                           f"{u['id']} {u['kind']} {e - s:.1f}s"))
    if (sheet := _sheet(rows, dbg / "unexplained.jpg")) is not None:
        written.append(sheet)
    return written
