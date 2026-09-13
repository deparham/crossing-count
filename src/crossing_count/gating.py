"""M1 motion gate: time ranges containing any motion near each counting line.

Asymmetric costs drive every choice here. Keeping an empty second costs the
detector a second of compute. Dropping a second with a person in it is
unrecoverable: M2 never sees them, so the reviewer never sees them. So:
  * the gate region is a generous band around the line plus the mask zone;
  * the background starts from a median of frames sampled across the video,
    so people present when the video starts are not baked in as "ghosts";
  * any blob above a small area threshold keeps the frame;
  * ranges are padded and nearby ranges merged;
  * MOG2 learns slowly, so a person standing still stays foreground.

One decode pass serves every camera picture in a multi-camera export.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

from . import geometry as geo
from . import layout as lay
from . import overlay as ov
from . import render
from . import video as vid
from .config import (
    BoundGeometry,
    ConfigError,
    GateParams,
    SiteConfig,
    aspect_matches,
    bind,
    load_config,
)
from .util import fmt_hms, slugify, write_json_atomic

SCHEMA = "activity/3"
TOOL_VERSION = "0.3.0"
BOOTSTRAP_FRAMES = 31
BOOTSTRAP_SPAN_S = 600.0
MATCH_MIN = 0.5  # below this, a picture is not this camera
MATCH_WARN = 0.8  # below this, the burned-in line has probably moved


def mog2_learning_rate(absorb_s: float, fps: float, background_ratio: float = 0.9) -> float:
    """Learning rate at which a motionless object is absorbed after about `absorb_s`.

    MOG2 counts a pixel's modes as background, heaviest first, until their weights
    sum past `background_ratio`. A new constant value (a person standing still)
    becomes background once the old mode's weight, (1 - a)^n, falls below that.
    """
    n = max(1.0, absorb_s * fps)
    return float(1.0 - background_ratio ** (1.0 / n))


def build_roi_mask(
    geom: BoundGeometry, params: GateParams, shape: tuple[int, int], tile: lay.Tile
) -> NDArray[np.uint8]:
    roi = geo.rasterize_polyline_band(shape, geom.line, params.line_margin * geom.height)
    if geom.mask_zone is not None:
        zone = geo.rasterize_polygons(shape, [geom.mask_zone])
        roi = geo.u8(cv2.bitwise_or(roi, geo.dilate_mask(zone, params.zone_margin * geom.height)))
    if geom.gate_ignore_zones:
        roi[geo.rasterize_polygons(shape, geom.gate_ignore_zones) > 0] = 0
    # Stay inside this camera's picture, below its header bar (the clock changes every frame).
    clip = np.zeros(shape, dtype=np.uint8)
    clip[tile.y0 + tile.header_px : tile.y1, tile.x0 : tile.x1] = 255
    return geo.u8(cv2.bitwise_and(roi, clip))


@dataclass(frozen=True)
class FrameStat:
    t: float
    max_blob_px: int
    fg_frac: float
    active: bool


class MotionGate:
    """Per-frame motion test inside one camera's gate region."""

    def __init__(
        self,
        roi_full: NDArray[np.uint8],
        params: GateParams,
        analysed_fps: float,
        picture_area_px: int,
    ) -> None:
        self.params = params
        self.roi_full = roi_full
        ys, xs = np.nonzero(roi_full)
        if len(xs) == 0:
            raise ConfigError("gate region is empty; check the config geometry")
        pad = 6
        h, w = roi_full.shape
        y0, y1 = max(0, int(ys.min()) - pad), min(h, int(ys.max()) + 1 + pad)
        x0, x1 = max(0, int(xs.min()) - pad), min(w, int(xs.max()) + 1 + pad)
        self.crop = (slice(y0, y1), slice(x0, x1))
        self.roi = roi_full[self.crop] > 0
        self.roi_px = int(self.roi.sum())
        self.min_blob_px = max(1, int(round(params.min_blob_frac * picture_area_px)))
        self.alpha = mog2_learning_rate(params.absorb_s, analysed_fps)
        self.mog = cv2.createBackgroundSubtractorMOG2(
            history=500,
            varThreshold=params.var_threshold,
            detectShadows=False,  # shadows count as motion: a shadow on the line is a person
        )
        self.k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self.k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))

    def _prep(self, image: NDArray[np.uint8]) -> NDArray[np.uint8]:
        return geo.u8(cv2.GaussianBlur(image[self.crop], (5, 5), 0))

    def init_background(self, median: NDArray[np.uint8]) -> None:
        """Start the model from a people-free median frame (learning rate 1 = reinitialise)."""
        self.mog.apply(self._prep(median), learningRate=1.0)

    def foreground(self, image: NDArray[np.uint8]) -> NDArray[np.uint8]:
        fg = self.mog.apply(self._prep(image), learningRate=self.alpha)
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, self.k_open)  # speckle from compression
        fg = geo.u8(cv2.morphologyEx(fg, cv2.MORPH_CLOSE, self.k_close))  # rejoin limbs
        fg[~self.roi] = 0
        return fg

    def process(self, t: float, image: NDArray[np.uint8]) -> FrameStat:
        fg = self.foreground(image)
        n, _, stats, _ = cv2.connectedComponentsWithStats(fg, connectivity=8)
        max_blob = int(stats[1:, cv2.CC_STAT_AREA].max()) if n > 1 else 0
        fg_frac = float(np.count_nonzero(fg)) / self.roi_px
        active = t < self.params.warmup_s or max_blob >= self.min_blob_px
        return FrameStat(t, max_blob, fg_frac, active)


def build_ranges(
    times: NDArray[np.float64],
    active: NDArray[np.bool_],
    frame_dt: float,
    pre_roll_s: float,
    post_roll_s: float,
    merge_gap_s: float,
    duration_s: float,
) -> list[tuple[float, float]]:
    """Turn per-frame activity into padded, merged [start, end) ranges in seconds."""
    runs: list[tuple[float, float]] = []
    start: float | None = None
    prev = 0.0
    for t, a in zip(times.tolist(), active.tolist()):
        if a:
            if start is None:
                start = t
            prev = t
        elif start is not None:
            runs.append((start, prev))
            start = None
    if start is not None:
        runs.append((start, prev))

    half = frame_dt / 2
    merged: list[tuple[float, float]] = []
    for s, e in runs:
        s2 = max(0.0, s - half - pre_roll_s)
        e2 = min(duration_s, e + half + post_roll_s)
        if merged and s2 - merged[-1][1] < merge_gap_s:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e2))
        else:
            merged.append((s2, e2))
    return merged


def eliminated_pct(ranges: list[tuple[float, float]], duration: float) -> float:
    active = sum(e - s for s, e in ranges)
    return 100.0 * (1.0 - active / duration) if duration > 0 else 0.0


def in_ranges(t: float, ranges: list[tuple[float, float]]) -> bool:
    return any(s <= t < e for s, e in ranges)


@dataclass
class Placement:
    """Which camera picture a config belongs to, and how well its line matched."""

    cfg: SiteConfig
    tile: lay.Tile
    score: float | None
    scores: dict[int, float | None]
    warnings: list[str]


def locate_cameras(
    cfgs: list[SiteConfig],
    tiles: list[lay.Tile],
    median: NDArray[np.uint8],
    tile_map: dict[str, int] | None = None,
) -> list[Placement]:
    """Assign each config to a camera picture, by explicit mapping or overlay match."""
    placements: list[Placement] = []
    for cfg in cfgs:
        scores: dict[int, float | None] = {}
        for t in tiles:
            if not aspect_matches(cfg, t.width, t.height):
                continue
            g = bind(cfg, t.width, t.height, t.origin)
            scores[t.index] = (
                ov.line_match_score(median, g.line, cfg.overlay_hsv) if cfg.overlay_hsv else None
            )
        if not scores:
            raise ConfigError(
                f"{cfg.sensor}: no camera picture in this video has the shape it was traced on "
                f"({(cfg.traced_on or {}).get('tile_size')}); retrace it for this layout"
            )
        listing = ", ".join(
            f"#{i}: {'n/a' if s is None else f'{s:.0%}'}" for i, s in scores.items()
        )
        # A video without the sensor's marks (a clean export) shows no burned-in line to
        # match, so fall back to the picture the config was drawn on.
        drawn_on = (cfg.traced_on or {}).get("tile_index")
        no_line_here = all((s or 0.0) < MATCH_MIN for s in scores.values())
        fallback = None
        if tile_map and cfg.sensor in tile_map:
            idx = tile_map[cfg.sensor]
            if idx not in scores:
                raise ConfigError(
                    f"--tile {cfg.sensor}={idx}: no usable picture #{idx} "
                    f"(pictures: {sorted(scores)})"
                )
        elif len(scores) == 1:
            idx = next(iter(scores))
        elif isinstance(drawn_on, int) and drawn_on in scores and no_line_here:
            idx = drawn_on
            fallback = (
                f"{cfg.sensor}: no burned-in counting line in this video, so using picture "
                f"#{idx}, the one it was drawn on. If the cameras are in a different order "
                f"here, pass --tile {cfg.sensor}=N."
            )
        elif cfg.overlay_hsv is None:
            raise ConfigError(
                f"{len(tiles)} camera pictures in this video and {cfg.sensor} has no recorded "
                f"overlay colour to tell which is its own. Pass --tile {cfg.sensor}=N "
                f"(pictures numbered from 0, left to right then top to bottom)."
            )
        else:
            ranked = sorted(scores.items(), key=lambda kv: (-(kv[1] or 0.0), kv[0]))
            idx, best = ranked[0]
            second = ranked[1][1] or 0.0
            if (best or 0.0) < MATCH_MIN or (best or 0.0) - second < 0.2:
                raise ConfigError(
                    f"cannot tell which picture is {cfg.sensor} (line-overlay match {listing}). "
                    f"Pass --tile {cfg.sensor}=N."
                )
        score = scores[idx]
        warnings: list[str] = [fallback] if fallback else []
        if fallback is None and score is not None and score < MATCH_WARN:
            warnings.append(
                f"{cfg.sensor}: only {score:.0%} of the traced line sits on the burned-in line "
                f"in picture #{idx}. The sensor's line may have moved since tracing, or this is "
                f"the wrong camera. Check debug/gate_region.png and retrace if needed."
            )
        placements.append(Placement(cfg, tiles[idx], score, scores, warnings))
    return placements


@dataclass
class CameraRun:
    cfg: SiteConfig
    params: GateParams
    placement: Placement
    tile: lay.Tile  # at analysis scale
    geom: BoundGeometry  # analysis-scale frame coordinates
    gate: MotionGate
    stats: list[FrameStat] = field(default_factory=list)

    def arrays(self) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64],
                              NDArray[np.bool_]]:
        return (
            np.array([s.t for s in self.stats], dtype=np.float64),
            np.array([s.max_blob_px for s in self.stats], dtype=np.float64),
            np.array([s.fg_frac for s in self.stats], dtype=np.float64),
            np.array([s.active for s in self.stats], dtype=bool),
        )


@dataclass
class GateRun:
    info: vid.VideoInfo
    audit: vid.TimebaseAudit
    tiles: list[lay.Tile]
    cameras: list[CameraRun]
    stride: int
    analysed_fps: float
    scale: float
    proc_size: tuple[int, int]
    duration_s: float
    sample_s: float | None
    median: NDArray[np.uint8]  # analysis scale
    wall_s: float


def run_gate(
    video_path: str | Path,
    config_paths: list[str | Path],
    *,
    tile_map: dict[str, int] | None = None,
    sample_s: float | None = None,
    overrides: dict[str, Any] | None = None,
    progress: bool = False,
) -> GateRun:
    cfgs = [load_config(p) for p in config_paths]
    sensors = [c.sensor for c in cfgs]
    if len(set(sensors)) != len(sensors):
        raise ConfigError(f"duplicate sensor names among configs: {sensors}")
    info = vid.probe(video_path)
    audit = vid.audit_timebase(video_path)
    duration = audit.duration_s if sample_s is None else min(audit.duration_s, sample_s)

    # People-free median frame: finds the pictures, identifies cameras, seeds the background.
    span = max(0.0, min(duration, BOOTSTRAP_SPAN_S) - 2 * audit.median_interval_s)
    boot = vid.grab_frames_at(video_path, np.linspace(0.0, span, BOOTSTRAP_FRAMES).tolist())
    median = lay.median_image([f.image for f in boot])
    tiles = lay.detect_tiles_in(median)
    placements = locate_cameras(cfgs, tiles, median, tile_map)

    params = [p.cfg.gate.with_overrides(**(overrides or {})) for p in placements]
    fps = audit.effective_fps
    stride = min(max(1, int(round(fps / pr.proc_fps))) for pr in params)
    scale = max(min(1.0, pr.proc_width / pl.tile.width) for pr, pl in zip(params, placements))
    if scale >= 0.999:
        scale, size = 1.0, None
        pw, ph = info.width, info.height
    else:
        pw = int(round(info.width * scale / 2) * 2)
        ph = int(round(info.height * scale / 2) * 2)
        size = (pw, ph)
    analysed_fps = fps / stride
    median_s = median if size is None else geo.u8(
        cv2.resize(median, size, interpolation=cv2.INTER_AREA))

    cams: list[CameraRun] = []
    for pl, pr in zip(placements, params):
        t_s = pl.tile.scaled(scale)
        geom = bind(pl.cfg, t_s.width, t_s.height, t_s.origin)
        roi = build_roi_mask(geom, pr, (ph, pw), t_s)
        gate = MotionGate(roi, pr, analysed_fps, t_s.width * t_s.height)
        gate.init_background(median_s)
        cams.append(CameraRun(pl.cfg, pr, pl, t_s, geom, gate))

    wall0 = time.monotonic()
    next_report = 0.0
    for fr in vid.iter_frames(video_path, end_s=sample_s, stride=stride, size=size):
        for cam in cams:
            cam.stats.append(cam.gate.process(fr.t, fr.image))
        if progress and fr.t >= next_report:
            elapsed = time.monotonic() - wall0
            speed = fr.t / elapsed if elapsed > 0 else 0.0
            print(f"\r  gating {fmt_hms(fr.t)} / {fmt_hms(duration)}  ({speed:5.1f}x realtime)",
                  end="", file=sys.stderr, flush=True)
            next_report = fr.t + max(1.0, duration / 100)
    wall = time.monotonic() - wall0
    if progress:
        print(file=sys.stderr)

    return GateRun(info, audit, tiles, cams, stride, analysed_fps, scale, (pw, ph), duration,
                   sample_s, median_s, wall)


def camera_result(run: GateRun, cam: CameraRun) -> dict[str, Any]:
    """activity.json for one camera. Deterministic for a given input (no wall times)."""
    times, max_blob, fg_frac, active = cam.arrays()
    p = cam.params
    frame_dt = run.stride / run.audit.effective_fps
    ranges = build_ranges(times, active, frame_dt, p.pre_roll_s, p.post_roll_s, p.merge_gap_s,
                          run.duration_s)
    active_s = sum(e - s for s, e in ranges)
    post_warm = times >= p.warmup_s
    blobs = max_blob[post_warm] if post_warm.any() else np.zeros(1)

    sensitivity = []
    for factor in (0.25, 0.5, 1.0, 2.0, 4.0):
        thr = max(1, int(round(cam.gate.min_blob_px * factor)))
        act = (times < p.warmup_s) | (max_blob >= thr)
        rr = build_ranges(times, act, frame_dt, p.pre_roll_s, p.post_roll_s, p.merge_gap_s,
                          run.duration_s)
        sensitivity.append({"min_blob_px": thr, "eliminated_pct": round(eliminated_pct(rr, run.duration_s), 2)})

    # Unpadded stretches of actual motion (not the forced warm-up); M2 compares these
    # with its proposals to find likely misses.
    moving = max_blob >= cam.gate.min_blob_px
    runs = build_ranges(times, moving, frame_dt, 0.0, 0.0, 1.0, run.duration_s)

    warnings = list(cam.geom.warnings) + list(cam.placement.warnings)
    fps_w = vid.fps_mismatch_warning(run.audit.effective_fps, cam.cfg.fps_assumed)
    if fps_w:
        warnings.append(fps_w)
    tile = cam.placement.tile
    return {
        "schema": SCHEMA,
        "tool_version": TOOL_VERSION,
        "video": run.info.as_dict(),
        "config": {
            "path": cam.cfg.source,
            "sha256": cam.cfg.sha256,
            "site": cam.cfg.site,
            "sensor": cam.cfg.sensor,
            "rule": cam.cfg.rule,
        },
        "picture": {
            "tile": tile.as_dict(),
            "overlay_match": None if cam.placement.score is None else round(cam.placement.score, 3),
            "overlay_match_all": {
                str(k): None if v is None else round(v, 3) for k, v in cam.placement.scores.items()
            },
        },
        "timebase": run.audit.as_dict(),
        "warnings": warnings,
        "sample_s": run.sample_s,
        "params": {
            **p.as_dict(),
            "stride": run.stride,
            "analysed_fps": round(run.analysed_fps, 4),
            "analysis_scale": round(run.scale, 4),
            "mog2_learning_rate": float(f"{cam.gate.alpha:.6g}"),
            "min_blob_px": cam.gate.min_blob_px,
            "gate_region_px": cam.gate.roi_px,
            "gate_region_frac_of_picture": round(cam.gate.roi_px / (cam.tile.width * cam.tile.height), 4),
        },
        "processed_duration_s": round(run.duration_s, 3),
        "ranges": [{"start_s": round(s, 3), "end_s": round(e, 3)} for s, e in ranges],
        "motion_runs": [{"start_s": round(s, 3), "end_s": round(e, 3)} for s, e in runs],
        "summary": {
            "n_ranges": len(ranges),
            "frames_analysed": len(times),
            "active_s": round(active_s, 3),
            "eliminated_s": round(run.duration_s - active_s, 3),
            "eliminated_pct": round(eliminated_pct(ranges, run.duration_s), 2),
        },
        "diagnostics": {
            "max_blob_px_percentiles": {
                f"p{q}": int(np.percentile(blobs, q)) for q in (50, 90, 99, 100)
            },
            "inactive_frames_over_half_threshold": int(
                ((~active) & (max_blob >= cam.gate.min_blob_px / 2)).sum()
            ),
            "large_change_frames": int((fg_frac > 0.5).sum()),
            "threshold_sensitivity": sensitivity,
        },
    }


def layout_result(run: GateRun) -> dict[str, Any]:
    return {
        "video": run.info.filename,
        "fingerprint": run.info.fingerprint,
        "pictures": [t.as_dict() for t in run.tiles],
        "cameras": [
            {
                "sensor": c.cfg.sensor,
                "picture": c.placement.tile.index,
                "overlay_match": None if c.placement.score is None else round(c.placement.score, 3),
            }
            for c in run.cameras
        ],
    }


def camera_dir(out_dir: Path, sensor: str) -> Path:
    return out_dir / slugify(sensor)


def write_outputs(run: GateRun, out_dir: Path) -> dict[str, tuple[Path, dict[str, Any]]]:
    write_json_atomic(out_dir / "layout.json", layout_result(run))
    written: dict[str, tuple[Path, dict[str, Any]]] = {}
    for cam in run.cameras:
        res = camera_result(run, cam)
        path = camera_dir(out_dir, cam.cfg.sensor) / "activity.json"
        write_json_atomic(path, res)
        written[cam.cfg.sensor] = (path, res)
    return written


def write_debug(
    run: GateRun,
    cam: CameraRun,
    result: dict[str, Any],
    video_path: str | Path,
    out_dir: Path,
    seed: int = 0,
    n_samples: int = 12,
) -> list[Path]:
    """Images for checking the gate by eye. Deterministic for a given seed."""
    dbg = out_dir / "debug"
    dbg.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    size = None if run.scale == 1.0 else run.proc_size
    times, max_blob, _, _ = cam.arrays()
    ranges = [(r["start_s"], r["end_s"]) for r in result["ranges"]]
    thr = cam.gate.min_blob_px

    def overlay(img: NDArray[np.uint8], text: str) -> NDArray[np.uint8]:
        drawn = render.draw_geometry(img, cam.geom, cam.gate.roi_full)
        return render.label(cam.tile.crop(drawn), text)

    p = dbg / "gate_region.png"
    cv2.imwrite(str(p), overlay(run.median, f"{cam.cfg.sensor}: gate region (tinted), median frame"))
    written.append(p)

    p = dbg / "timeline.png"
    cv2.imwrite(str(p), render.timeline_image(times, max_blob, thr, ranges, run.duration_s,
                                              cam.params.warmup_s))
    written.append(p)

    rng = np.random.default_rng(seed)
    elim_mask = np.array([not in_ranges(t, ranges) for t in times.tolist()], dtype=bool)

    # Closest calls: the highest-motion frames the gate discarded, >= 10 s apart.
    picked: list[float] = []
    for i in np.argsort(-max_blob, kind="stable"):
        if not elim_mask[i] or max_blob[i] <= 0:
            continue
        t = float(times[i])
        if all(abs(t - q) >= 10 for q in picked):
            picked.append(t)
        if len(picked) >= n_samples:
            break
    elim_t = times[elim_mask].tolist()
    rand = sorted(rng.choice(elim_t, size=min(n_samples, len(elim_t)), replace=False).tolist()) \
        if elim_t else []
    act_t = times[(~elim_mask) & (times >= cam.params.warmup_s)].tolist()
    rand_act = sorted(rng.choice(act_t, size=min(n_samples, len(act_t)), replace=False).tolist()) \
        if act_t else []

    blob_at = dict(zip(times.tolist(), max_blob.tolist()))
    for name, sample_times, title in (
        ("eliminated_closest_calls.jpg", sorted(picked), "ELIMINATED"),
        ("eliminated_random.jpg", rand, "ELIMINATED"),
        ("active_random.jpg", rand_act, "ACTIVE"),
    ):
        if not sample_times:
            continue
        grabbed = vid.grab_frames_at(video_path, sample_times, size)
        tiles = [
            overlay(f.image, f"{fmt_hms(f.t)} {title} blob={int(blob_at.get(t, 0))}/{thr}px")
            for f, t in zip(grabbed, sample_times)
        ]
        p = dbg / name
        cv2.imwrite(str(p), render.contact_sheet(tiles), [cv2.IMWRITE_JPEG_QUALITY, 88])
        written.append(p)
    return written
