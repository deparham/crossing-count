#!/usr/bin/env python3
"""Trace one camera's counting line and zones by clicking on its picture.

    uv run trace_line.py VIDEO --site CN-146 --sensor CN-146-PB2 [--tile N]
    uv run trace_line.py VIDEO --edit sites/cn-146-pb2.json

The picture shown is a median of frames sampled across the video, so people
are removed and the burned-in overlay is easy to follow (F toggles a live
frame). Click on the burned-in overlay; the magnifier helps with precision.
Steps: counting line -> inside side -> mask zone -> filter zone(s) ->
exclusion zone(s) -> gate-ignore zone(s). Skip any zone the camera lacks (S).

Keys: Enter = finish step, Backspace = undo point, N = next zone, S = skip,
      B = back a step, R = redo step, F = live frame / median, Esc = quit.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

from crossing_count import layout as lay
from crossing_count import overlay as ov
from crossing_count import render, tracing
from crossing_count import video as vid
from crossing_count.config import (
    ConfigError,
    SiteConfig,
    aspect_matches,
    bind,
    load_config,
    parse_config,
)
from crossing_count.util import slugify, write_json_atomic

Point = tuple[float, float]
Image = NDArray[np.uint8]
WINDOW = "trace_line"
BANNER_H = 96
CURSOR = (0, 255, 255)


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    help: str
    required: bool
    multi: bool
    min_pts: int
    closed: bool
    color: tuple[int, int, int]


STEPS = (
    Step("line", "COUNTING LINE",
         "Click along the burned-in counting line (light blue, with triangles), one point per "
         "bend, in order. Enter when done.", True, False, 2, False, render.LINE),
    Step("inside", "INSIDE",
         "Click once on the store side of the line (the side the triangles point to).",
         True, False, 1, False, render.LINE),
    Step("mask_zone", "MASK ZONE",
         "Click the corners of the grey mask band. Enter when done. S = this camera has no "
         "mask zone.", False, False, 3, True, render.MASK),
    Step("filter_zones", "FILTER ZONE",
         "Click the corners of the filter zone. N = start another. Enter when done. S = none.",
         False, True, 3, True, render.FILTER),
    Step("exclusion_zones", "EXCLUSION ZONE",
         "Optional: crossings touching these are flagged for review. N = another, Enter = done, "
         "S = none.", False, True, 3, True, render.EXCLUSION),
    Step("gate_ignore_zones", "GATE-IGNORE ZONE",
         "Optional, rarely needed: motion here is never a person (screens, doors). N / Enter / "
         "S = none.", False, True, 3, True, render.IGNORE),
)
KEYS_HELP = "Enter=done  Backspace=undo  N=next zone  S=skip  B=back  R=redo step  F=live/median  Esc=quit"


def _wrap(text: str, width_px: int, scale: float = 0.5) -> list[str]:
    lines: list[str] = []
    cur = ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if cv2.getTextSize(trial, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)[0][0] > width_px and cur:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


class Tracer:
    def __init__(self, median: Image, live: Image, scale: float,
                 preset: dict[str, list[list[Point]]] | None) -> None:
        self.median, self.live = median, live
        self.scale = scale
        self.show_live = False
        self.done: dict[str, list[list[Point]]] = {s.key: [] for s in STEPS}
        if preset:
            self.done.update(preset)
        self.current: list[Point] = []
        self.step = 0
        self.cursor: Point | None = None
        self.message = ""
        self.finished = False
        self.quit = False

    @property
    def s(self) -> Step:
        return STEPS[self.step]

    # --- input -----------------------------------------------------------------------
    def on_mouse(self, event: int, x: int, y: int, flags: int, param: Any) -> None:
        if y < BANNER_H:
            return
        p = (x / self.scale, (y - BANNER_H) / self.scale)
        if event == cv2.EVENT_MOUSEMOVE:
            self.cursor = p
        elif event == cv2.EVENT_LBUTTONDOWN and not self.finished:
            self.message = ""
            if self.s.key == "inside":
                self.done["inside"] = [[p]]
                self._advance()
            else:
                self.current.append(p)

    def handle_key(self, key: int) -> None:
        if key < 0:
            return
        k = key & 0xFF
        ch = chr(k).lower() if 32 <= k < 127 else ""
        if k in (13, 10):
            self._enter()
        elif k in (8, 127):
            self._undo()
        elif k == 27:
            self.quit = True
        elif ch == "n":
            self._next_zone()
        elif ch == "s":
            self._skip()
        elif ch == "b":
            self.current = []
            self.step = max(0, self.step - 1)
            self.message = ""
        elif ch == "r":
            self.done[self.s.key] = []
            self.current = []
        elif ch == "f":
            self.show_live = not self.show_live

    def _commit(self) -> bool:
        pts = tracing.dedupe(self.current)
        if len(pts) < self.s.min_pts:
            self.message = f"{self.s.title} needs at least {self.s.min_pts} distinct points"
            return False
        if self.s.multi:
            self.done[self.s.key].append(pts)
        else:
            self.done[self.s.key] = [pts]
        self.current = []
        return True

    def _enter(self) -> None:
        if self.current:
            if not self._commit():
                return
        elif self.s.required and not self.done[self.s.key]:
            self.message = f"{self.s.title} is required"
            return
        self._advance()

    def _next_zone(self) -> None:
        if not self.s.multi:
            self.message = f"{self.s.title} is a single shape; press Enter when done"
        elif self.current:
            self._commit()

    def _skip(self) -> None:
        if self.s.required:
            self.message = f"{self.s.title} is required and cannot be skipped"
            return
        self.done[self.s.key] = []
        self.current = []
        self._advance()

    def _undo(self) -> None:
        if self.current:
            self.current.pop()
        elif self.done[self.s.key] and self.s.key != "inside":
            self.current = list(self.done[self.s.key].pop())[:-1]

    def _advance(self) -> None:
        self.current = []
        if self.step + 1 >= len(STEPS):
            self.finished = True
        else:
            self.step += 1

    def reopen(self, key: str, message: str) -> None:
        self.finished = False
        self.step = next(i for i, s in enumerate(STEPS) if s.key == key)
        self.message = message

    # --- drawing ---------------------------------------------------------------------
    def _p(self, p: Point) -> tuple[int, int]:
        return (int(round(p[0] * self.scale)), int(round(p[1] * self.scale)))

    def render(self) -> Image:
        base = self.live if self.show_live else self.median
        disp = np.asarray(cv2.resize(base, None, fx=self.scale, fy=self.scale,
                                     interpolation=cv2.INTER_LINEAR), dtype=np.uint8)
        for i, st in enumerate(STEPS):
            width = 2 if i == self.step and not self.finished else 1
            for shape in self.done[st.key]:
                if st.key == "inside":
                    c = self._p(shape[0])
                    cv2.drawMarker(disp, c, st.color, cv2.MARKER_CROSS, 16, 2)
                    cv2.putText(disp, "inside", (c[0] + 8, c[1] - 6), cv2.FONT_HERSHEY_SIMPLEX,
                                0.45, st.color, 1, cv2.LINE_AA)
                    continue
                pts = np.array([self._p(p) for p in shape], dtype=np.int32)
                cv2.polylines(disp, [pts], st.closed, st.color, width, cv2.LINE_AA)
                for q in pts:
                    cv2.circle(disp, (int(q[0]), int(q[1])), 3, st.color, -1, cv2.LINE_AA)
        if self.current and not self.finished:
            cur = [self._p(p) for p in self.current]
            cv2.polylines(disp, [np.array(cur, dtype=np.int32)], False, CURSOR, 1, cv2.LINE_AA)
            for q in cur:
                cv2.circle(disp, q, 3, CURSOR, -1, cv2.LINE_AA)
            if self.cursor is not None:
                cv2.line(disp, cur[-1], self._p(self.cursor), CURSOR, 1, cv2.LINE_AA)
                if self.s.closed and len(cur) >= 2:
                    cv2.line(disp, self._p(self.cursor), cur[0], CURSOR, 1, cv2.LINE_AA)

        if self.cursor is not None:
            mag = _magnifier(disp, self._p(self.cursor), radius=int(14 * self.scale))
            mh, mw = mag.shape[:2]
            x0 = 8 if self.cursor[0] * self.scale > disp.shape[1] / 2 else disp.shape[1] - mw - 8
            disp[8 : 8 + mh, x0 : x0 + mw] = mag

        banner = np.zeros((BANNER_H, disp.shape[1], 3), dtype=np.uint8)
        title = "DONE - saving" if self.finished else (
            f"Step {self.step + 1}/{len(STEPS)}: {self.s.title}"
            + ("  (live frame)" if self.show_live else "  (median of frames, people removed)"))
        cv2.putText(banner, title, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1,
                    cv2.LINE_AA)
        y = 40
        if not self.finished:
            for line in _wrap(self.s.help, disp.shape[1] - 16)[:2]:
                cv2.putText(banner, line, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 220, 220),
                            1, cv2.LINE_AA)
                y += 18
        cv2.putText(banner, KEYS_HELP, (8, BANNER_H - 22), cv2.FONT_HERSHEY_SIMPLEX, 0.42,
                    (160, 160, 160), 1, cv2.LINE_AA)
        if self.message:
            cv2.putText(banner, self.message, (8, BANNER_H - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        (80, 80, 255), 1, cv2.LINE_AA)
        return np.asarray(np.vstack([banner, disp]), dtype=np.uint8)


def _magnifier(img: Image, c: tuple[int, int], radius: int = 28, out: int = 220) -> Image:
    pad = cv2.copyMakeBorder(img, radius, radius, radius, radius, cv2.BORDER_CONSTANT)
    crop = pad[c[1] : c[1] + 2 * radius + 1, c[0] : c[0] + 2 * radius + 1]
    big = np.asarray(cv2.resize(crop, (out, out), interpolation=cv2.INTER_NEAREST), dtype=np.uint8)
    m = out // 2
    cv2.line(big, (m, 0), (m, out), CURSOR, 1)
    cv2.line(big, (0, m), (out, m), CURSOR, 1)
    cv2.rectangle(big, (0, 0), (out - 1, out - 1), (255, 255, 255), 1)
    return big


def pick_tile(median: Image, tiles: list[lay.Tile]) -> lay.Tile | None:
    scale = min(1.0, 1400 / median.shape[1], 860 / median.shape[0])
    base = cv2.resize(median, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    for t in tiles:
        cv2.rectangle(base, (int(t.x0 * scale), int(t.y0 * scale)),
                      (int(t.x1 * scale) - 1, int(t.y1 * scale) - 1), (0, 255, 255), 2)
        cv2.putText(base, f"#{t.index}", (int(t.x0 * scale) + 10, int((t.y0 + t.height / 2) * scale)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(base, "Click the camera picture to trace (Esc = quit)", (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2, cv2.LINE_AA)
    chosen: list[lay.Tile] = []

    def on_mouse(event: int, x: int, y: int, flags: int, param: Any) -> None:
        if event == cv2.EVENT_LBUTTONDOWN:
            for t in tiles:
                if t.contains(x / scale, y / scale):
                    chosen.append(t)

    cv2.namedWindow(WINDOW, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(WINDOW, on_mouse)
    while not chosen:
        cv2.imshow(WINDOW, base)
        if (cv2.waitKey(20) & 0xFF) == 27:
            return None
    return chosen[0]


def shapes_from_config(cfg: SiteConfig, tile: lay.Tile) -> dict[str, list[list[Point]]]:
    g = bind(cfg, tile.width, tile.height)

    def pts(a: NDArray[np.float64]) -> list[Point]:
        return [(float(x), float(y)) for x, y in a]

    line = pts(g.line)
    return {
        "line": [line],
        "inside": [[tracing.inside_point_for(line, g.inside_sign)]],
        "mask_zone": [pts(g.mask_zone)] if g.mask_zone is not None else [],
        "filter_zones": [pts(z) for z in g.filter_zones],
        "exclusion_zones": [pts(z) for z in g.exclusion_zones],
        "gate_ignore_zones": [pts(z) for z in g.gate_ignore_zones],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("video", type=Path)
    ap.add_argument("--site")
    ap.add_argument("--sensor", help="sensor / camera name, e.g. CN-146-PB2")
    ap.add_argument("--tile", type=int, help="camera picture number (0 = top-left); asked if omitted")
    ap.add_argument("--edit", type=Path, help="start from an existing config and overwrite it")
    ap.add_argument("--out", type=Path, help="default: sites/<sensor>.json")
    ap.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    ap.add_argument("--at", type=float, help="time of the live frame shown with F (default: middle)")
    ap.add_argument("--min-dwell", type=float, help="min_dwell_in_zone_s (default 0.4)")
    ap.add_argument("--pending-timeout", type=float, help="pending_timeout_s (default 20)")
    ap.add_argument("--stitch-gap", type=float, help="stitch_gap_max_s (default 1.5)")
    args = ap.parse_args(argv)

    if not args.video.is_file():
        ap.error(f"video not found: {args.video}")
    preset = load_config(args.edit) if args.edit else None
    site = args.site or (preset.site if preset else None)
    sensor = args.sensor or (preset.sensor if preset else None)
    if not site or not sensor:
        ap.error("--site and --sensor are required (or --edit an existing config)")
    out: Path = args.out or args.edit or Path("sites") / f"{slugify(sensor)}.json"
    if out.exists() and not args.force and out != args.edit:
        ap.error(f"{out} exists; use --edit {out} to retrace it, or --force to replace it")

    print("reading video (median of 25 frames) ...")
    audit = vid.audit_timebase(args.video)
    span = max(0.0, min(audit.duration_s, 600.0) - 0.3)
    frames = vid.grab_frames_at(args.video, np.linspace(0.0, span, 25).tolist())
    median = lay.median_image([f.image for f in frames])
    tiles = lay.detect_tiles_in(median)
    at = args.at if args.at is not None else audit.duration_s / 2
    live = vid.grab_frames_at(args.video, [min(at, audit.duration_s - 0.3)])[0].image
    print(f"{len(tiles)} camera picture(s) found")

    tile: lay.Tile | None
    if args.tile is not None:
        if not 0 <= args.tile < len(tiles):
            ap.error(f"--tile must be 0..{len(tiles) - 1}")
        tile = tiles[args.tile]
    elif len(tiles) == 1:
        tile = tiles[0]
    elif preset is not None and preset.overlay_hsv is not None:
        scored = [
            (ov.line_match_score(median, bind(preset, t.width, t.height, t.origin).line,
                                 preset.overlay_hsv), t)
            for t in tiles if aspect_matches(preset, t.width, t.height)
        ]
        tile = max(scored, key=lambda st: st[0])[1] if scored else pick_tile(median, tiles)
    else:
        tile = pick_tile(median, tiles)
    if tile is None:
        print("quit without saving")
        return 1

    tile_med, tile_live = tile.crop(median).copy(), tile.crop(live).copy()
    preset_shapes = shapes_from_config(preset, tile) if preset else None
    scale = min(1400 / tile.width, 860 / tile.height, 3.0)
    tracer = Tracer(tile_med, tile_live, scale, preset_shapes)
    cv2.namedWindow(WINDOW, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(WINDOW, tracer.on_mouse)

    min_dwell = args.min_dwell or (preset.min_dwell_in_zone_s if preset else 0.4)
    pending = args.pending_timeout or (preset.pending_timeout_s if preset else 20.0)
    stitch = args.stitch_gap or (preset.stitch_gap_max_s if preset else 1.5)
    raw: dict[str, Any] = {}
    warnings: list[str] = []
    while True:
        cv2.imshow(WINDOW, tracer.render())
        key = cv2.waitKey(20)
        if cv2.getWindowProperty(WINDOW, cv2.WND_PROP_VISIBLE) < 1:
            tracer.quit = True
        tracer.handle_key(key)
        if tracer.quit:
            cv2.destroyAllWindows()
            print("quit without saving")
            return 1
        if not tracer.finished:
            continue
        d = tracer.done
        line = d["line"][0]
        try:
            raw, warnings = tracing.build_config_dict(
                site=site, sensor=sensor, tile_size=(tile.width, tile.height),
                line=line, inside_point=d["inside"][0][0],
                mask_zone=d["mask_zone"][0] if d["mask_zone"] else None,
                filter_zones=d["filter_zones"], exclusion_zones=d["exclusion_zones"],
                gate_ignore_zones=d["gate_ignore_zones"], fps=audit.effective_fps,
                overlay_hsv=ov.overlay_color_from_line(tile_med, np.asarray(line)),
                traced_on={"video": args.video.name, "tile_index": tile.index,
                           "tile_size": [tile.width, tile.height],
                           "date": datetime.now().astimezone().date().isoformat()},
                min_dwell_in_zone_s=min_dwell, pending_timeout_s=pending, stitch_gap_max_s=stitch,
                notes=f"traced from '{args.video.name}' picture #{tile.index}",
                gate=(preset.to_json_dict().get("gate") if preset else None),
            )
        except ConfigError as exc:
            tracer.reopen("mask_zone" if "mask" in str(exc) else "line", f"Problem: {exc}")
            continue
        break
    cv2.destroyAllWindows()

    write_json_atomic(out, raw)
    cfg = parse_config(raw)
    preview = Path("runs") / "trace_previews" / f"{slugify(sensor)}.png"
    preview.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(preview), render.draw_geometry(tile_med, bind(cfg, tile.width, tile.height)))
    print(f"wrote {out}  (rule: {cfg.rule}, inside: {cfg.inside_side}, "
          f"overlay colour: {cfg.overlay_hsv or 'none found'})")
    print(f"preview {preview}")
    if cfg.overlay_hsv is None:
        print("note: no burned-in line colour found under the traced line; multi-camera videos "
              "will need --tile to place this config")
    for w in warnings:
        print(f"WARNING: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
