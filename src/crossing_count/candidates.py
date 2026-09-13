"""M2: candidate crossings for each camera, inside the M1 activity ranges.

Every count the rule would commit becomes a *candidate* for a person to accept,
reject or split. Nothing here is a final count. Every track the rule rejected
is kept, with its reason, and so is every stretch of motion near the line that
produced no proposal (the likely misses).

For busy scenes: tracks are cut where they jump further than a person can
walk (the tracker swapped people), broken tracks are only rejoined at walking
speed, same-direction counts moments apart at the same spot are merged, and a
track that stops just short of the line followed by one that starts just past
it is listed first among the unexplained, as a crossing the tracker lost.
"""

from __future__ import annotations

import bisect
import hashlib
import itertools
import json
import pickle
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from . import geometry as geo
from . import video as vid
from .config import BoundGeometry, ConfigError, SiteConfig, bind, load_config
from .crossing import IN, OUT, find_crossings
from .detector import (
    DerotatedDetector,
    Detection,
    Detector,
    NaiveDetector,
    load_model,
    pick_device,
    tracking_region,
)
from .gating import camera_dir
from .layout import Tile
from .rule import (
    DUPLICATE,
    PENDING_EXPIRED,
    Committed,
    Rejected,
    RuleParams,
    evaluate_track,
    smooth_path,
)
from .tracker import ByteTrackAdapter
from .tracks import EOF, LOST, RANGE_END, Track, TrackSample, split_at_jumps, stitch
from .util import fmt_hms, slugify, write_json_atomic

SCHEMA = "candidates/1"
TOOL_VERSION = "0.5.0"
CONCURRENCY_WINDOW_S = 2.0
RETURN_PAIR_WINDOW_S = 10.0  # an OUT then an IN (or the reverse) this close, on different
RETURN_PAIR_POSITION = 0.15  # tracks, at nearly the same place, may be one person returning
DUP_WINDOW_S = 1.5  # same-direction counts this close in time ...
DUP_POSITION = 0.06  # ... and along the line, on different tracks: one person counted twice
EXPIRED_ALARM = 0.02  # pending_expired above this share of committed counts: the rule is costly
CLIP_PAD_S = 2.0
CLIP_MAX_S = 30.0
# Between samples a track may move JUMP_BASE_FRAC picture heights (detection wobble) plus
# MAX_SPEED_FRAC picture heights per second; more is a swapped identity, not a walk. On
# RetailNext fisheye pictures people's speed over 1 s windows had p99 of 0.15-0.18 heights/s.
# Tighter than this lost real crossings on a busy clip, so the limit stays generous and
# BIG_JUMP_FRAC flags the rest for the reviewer instead of deleting them.
JUMP_BASE_FRAC = 0.08
MAX_SPEED_FRAC = 0.35
BIG_JUMP_FRAC = 0.20  # one step this long (picture heights) ...
BIG_JUMP_WINDOW_S = 3.0  # ... this close to the crossing: maybe two people glued into one track
FLAG_BIG_JUMP = "big_jump_near_line"
BROKEN_NEAR_FRAC = 0.10  # a track ending this close to the line ...
BROKEN_REACH_FRAC = 0.25  # ... and another starting this close to that end, past the line,
BROKEN_GAP_S = 3.0  # ... this soon after: probably one person lost while crossing
FLAG_POSSIBLE_RETURN = "possible_return"
MISS_KIND = "broken_track_at_line"


@dataclass(frozen=True)
class DetectOptions:
    mode: str = "derotated"  # or "naive"
    model: str = "yolo11s.pt"
    device: str | None = None
    det_fps: float = 10.0
    batch_frames: int = 4
    conf: float = 0.08
    track_high: float = 0.2  # ByteTrack: first-pass association threshold
    track_low: float = 0.08  # ByteTrack: weaker detections may still extend a track
    track_new: float = 0.2  # ByteTrack: a new track needs at least this confidence
    assoc_box: str = "full"  # "full" person boxes, or "compact" lower-body boxes
    record: bool = False  # keep every frame's detections, so tracking can be re-run fast

    def tracker_kw(self) -> dict[str, Any]:
        return {"high": self.track_high, "low": self.track_low, "new": self.track_new,
                "box": self.assoc_box}


DetectorFactory = Callable[[SiteConfig, BoundGeometry, Tile, NDArray[np.uint8]], Detector]


class RecordingDetector:
    """Passes detections through and keeps them, so tracking can be re-run without YOLO."""

    def __init__(self, inner: Detector) -> None:
        self.inner = inner
        self.name = inner.name
        self.frames: dict[float, list[Detection]] = {}
        for attr in ("crop_px", "imgsz", "device", "specs"):
            if hasattr(inner, attr):
                setattr(self, attr, getattr(inner, attr))

    def learn_static(self, median: NDArray[np.uint8]) -> int:
        learn = getattr(self.inner, "learn_static", None)
        return int(learn(median)) if learn else 0

    def detect(self, frames: Any, times: Any) -> list[list[Detection]]:
        out = self.inner.detect(frames, times)
        for t, dets in zip(times, out):
            self.frames[round(float(t), 3)] = dets
        return out


_NO_IMAGE = np.zeros((1, 1, 3), dtype=np.uint8)


def _recorded_frames(detector: Any, start: float, end: float,
                     stride: int = 1) -> Iterator[vid.Frame]:
    """Frame stubs at the recorded times: replaying detections needs no video decoding.

    A stride above 1 keeps every stride-th recorded frame, which simulates a lower
    detection rate without re-running the detector.
    """
    for k, t in enumerate(t for t in sorted(detector.frames) if start <= t < end):
        if k % stride == 0:
            yield vid.Frame(-1, t, _NO_IMAGE)


class ReplayDetector:
    """Serves detections saved by a --record run: tracking experiments in seconds."""

    needs_frames = False

    def __init__(self, path: Path) -> None:
        data = pickle.loads(path.read_bytes())
        self.frames: dict[float, list[Detection]] = data["frames"]
        self.name = f"replay:{data['mode']}:{data['model']}"

    def detect(self, frames: Any, times: Any) -> list[list[Detection]]:
        return [list(self.frames.get(round(float(t), 3), [])) for t in times]


def yolo_factory(opts: DetectOptions) -> DetectorFactory:
    model = load_model(opts.model)
    device = pick_device(opts.device)

    def make(cfg: SiteConfig, geom: BoundGeometry, tile: Tile,
             region: NDArray[np.uint8]) -> Detector:
        cls = DerotatedDetector if opts.mode == "derotated" else NaiveDetector
        det: Detector = cls(model, geom, tile, region, device, conf=opts.conf)
        return RecordingDetector(det) if opts.record else det

    return make


def replay_factory(run_dir: Path, mode: str = "derotated") -> DetectorFactory:
    def make(cfg: SiteConfig, geom: BoundGeometry, tile: Tile,
             region: NDArray[np.uint8]) -> Detector:
        return ReplayDetector(output_dir(run_dir, cfg.sensor, mode) / "detections.pkl")

    return make


def load_activity(run_dir: Path, cfg: SiteConfig, info: vid.VideoInfo,
                  sample_s: float | None) -> tuple[dict[str, Any], str]:
    path = camera_dir(run_dir, cfg.sensor) / "activity.json"
    if not path.is_file():
        raise ConfigError(f"{path} not found; run gate.py on this video with this config first")
    data = path.read_bytes()
    act = json.loads(data)
    if act["video"]["fingerprint"] != info.fingerprint:
        raise ConfigError(f"{path} was made from a different video; re-run gate.py")
    if act["config"]["sha256"] != cfg.sha256:
        raise ConfigError(f"{cfg.sensor}: the config changed since gate.py ran; re-run gate.py")
    if act.get("sample_s") != sample_s:
        raise ConfigError(f"{path} was made with --sample {act.get('sample_s')}; use the same "
                          f"--sample here or re-run gate.py")
    if "motion_runs" not in act:
        raise ConfigError(f"{path} is from an older gate.py; re-run gate.py")
    return act, hashlib.sha256(data).hexdigest()


def _subtract(spans: list[tuple[float, float]],
              cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    cuts = sorted(cuts)
    for s, e in spans:
        pieces = [(s, e)]
        for cs, ce in cuts:
            nxt: list[tuple[float, float]] = []
            for ps, pe in pieces:
                if ce <= ps or cs >= pe:
                    nxt.append((ps, pe))
                    continue
                if cs > ps:
                    nxt.append((ps, cs))
                if ce < pe:
                    nxt.append((ce, pe))
            pieces = nxt
        out.extend(pieces)
    return out


@dataclass
class CameraDetection:
    cfg: SiteConfig
    candidates: dict[str, Any]
    discarded: dict[str, Any]
    unexplained: dict[str, Any]
    tracks: list[Track]
    wall_s: float
    recorded: dict[str, Any] | None = None

    def tracks_json(self) -> dict[str, Any]:
        meta = {k: v for k, v in self.candidates.items() if k not in ("candidates", "summary")}
        return {**meta, "schema": "tracks/1", "tracks": [
            {"track_id": t.track_id, "ended_by": t.ended_by, "stitched_from": t.stitched_from,
             "samples": [[s.t, round(s.x, 1), round(s.y, 1), s.conf] for s in t.samples]}
            for t in self.tracks]}


class _RangeTracker:
    """Detection and ByteTrack over one activity range, fed in batches of frames."""

    def __init__(self, detector: Detector, analysed_fps: float, ids: Iterator[int],
                 tracker_kw: dict[str, Any]) -> None:
        self.detector = detector
        self.tracker = ByteTrackAdapter(analysed_fps=analysed_fps, **tracker_kw)
        self.live: dict[int, list[TrackSample]] = {}
        self.gid_of: dict[int, int] = {}  # tracker id -> id unique across ranges
        self.ids = ids
        self.det_times: list[float] = []
        self.det_counts: list[int] = []

    def feed(self, frames: list[vid.Frame]) -> None:
        if not frames:
            return
        per = self.detector.detect([f.image for f in frames], [f.t for f in frames])
        for f, dets in zip(frames, per):
            t = round(f.t, 3)
            self.det_times.append(t)
            self.det_counts.append(len(dets))
            for bt_id, di in self.tracker.update(dets):
                if bt_id not in self.gid_of:
                    self.gid_of[bt_id] = next(self.ids)
                d: Detection = dets[di]
                self.live.setdefault(self.gid_of[bt_id], []).append(
                    TrackSample(t, d.foot[0], d.foot[1], d.bbox, d.conf))


@dataclass
class TrackingResult:
    tracks: list[Track]
    det_times: list[float]
    det_counts: list[int]
    joins: int
    splits: int
    frame_dt: float


def track_camera(
    video_path: str | Path,
    act: dict[str, Any],
    detector: Detector,
    fps: float,
    det_fps: float,
    batch_frames: int,
    stitch_gap_s: float,
    stitch_radius_px: float,
    jump_base_px: float,
    max_speed_px_s: float,
    progress: bool = False,
    tracker_kw: dict[str, Any] | None = None,
) -> TrackingResult:
    """Detect and track inside each activity range; split at jumps; stitch short gaps."""
    stride = max(1, int(round(fps / det_fps)))
    frame_dt = stride / fps
    duration = float(act["processed_duration_s"])
    ranges = [(r["start_s"], r["end_s"]) for r in act["ranges"]]
    total = sum(e - s for s, e in ranges) or 1.0
    done = 0.0
    tracks: list[Track] = []
    det_times: list[float] = []
    det_counts: list[int] = []
    joins = splits = 0
    ids = itertools.count(1)
    wall0 = time.monotonic()

    for rs, re_ in ranges:
        rt = _RangeTracker(detector, fps / stride, ids, tracker_kw or {})
        batch: list[vid.Frame] = []
        source = (vid.iter_range(video_path, rs, re_, stride=stride)
                  if getattr(detector, "needs_frames", True)
                  else _recorded_frames(detector, rs, re_, stride))
        for fr in source:
            batch.append(fr)
            if len(batch) >= batch_frames:
                rt.feed(batch)
                batch = []
        rt.feed(batch)
        det_times.extend(rt.det_times)
        det_counts.extend(rt.det_counts)

        range_tracks: list[Track] = []
        for gid in sorted(rt.live):
            samples = rt.live[gid]
            if samples[-1].t >= re_ - 1.5 * frame_dt:
                ended = EOF if re_ >= duration - 1.5 * frame_dt else RANGE_END
            else:
                ended = LOST
            parts = split_at_jumps(Track(gid, samples, ended), jump_base_px, max_speed_px_s,
                                   lambda: next(ids))
            splits += len(parts) - 1
            range_tracks.extend(parts)
        joined, n = stitch(range_tracks, stitch_gap_s, stitch_radius_px, max_speed_px_s)
        tracks.extend(joined)
        joins += n
        done += re_ - rs
        if progress:
            el = time.monotonic() - wall0
            print(f"\r    tracking {100 * done / total:5.1f}% of active time "
                  f"({done / el if el else 0:4.1f}x realtime)", end="", file=sys.stderr, flush=True)
    if progress:
        print(file=sys.stderr)
    return TrackingResult(tracks, det_times, det_counts, joins, splits, frame_dt)


def _merge_duplicates(
    cands: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], dict[str, Any]]]]:
    """Same-direction counts moments apart at the same spot on different tracks.

    Keeps the most confident; returns (kept, [(duplicate, kept one it duplicates)]).
    """
    kept: list[dict[str, Any]] = []
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for c in cands:
        match = next(
            (k for k in reversed(kept)
             if abs(c["t_seconds"] - k["t_seconds"]) <= DUP_WINDOW_S
             and k["direction"] == c["direction"] and k["track_id"] != c["track_id"]
             and abs(k["line_position"] - c["line_position"]) <= DUP_POSITION),
            None)
        if match is None:
            kept.append(c)
        elif c["confidence"] > match["confidence"]:
            idx = next(i for i, k in enumerate(kept) if k is match)
            kept[idx] = c
            pairs = [(d, c if w is match else w) for d, w in pairs]
            pairs.append((match, c))
        else:
            pairs.append((c, match))
    kept.sort(key=lambda c: (c["t_seconds"], c["track_id"]))
    return kept, pairs


def _lost_at_line(tracks: list[Track], rp: RuleParams, with_outcome: set[int],
                  duration: float) -> list[dict[str, Any]]:
    """A track that crossed the line but was lost before it was clearly past it.

    The hysteresis stops jitter at the line counting as crossings, but it also
    means such a track produces no proposal; list it as a likely miss instead.
    """
    out: list[dict[str, Any]] = []
    for t in tracks:
        if t.ended_by != LOST or t.track_id in with_outcome or len(t.samples) < 2:
            continue
        pts = smooth_path(t.points(), rp.smooth_window)
        xs = find_crossings(t.times(), pts, rp.line, rp.inside_sign)
        if not xs:
            continue
        end = pts[-1]
        if geo.distance_to_polyline(rp.line, end) >= max(rp.hysteresis_px, 1.0):
            continue
        c = xs[-1]
        out.append({
            "id": "",
            "start_s": round(max(0.0, c.t - CLIP_PAD_S), 2),
            "end_s": round(min(duration, t.end_t + CLIP_PAD_S), 2),
            "kind": MISS_KIND,
            "direction_guess": c.direction,
            "t_seconds": round(c.t, 2),
            "tracks": [t.track_id],
            "detail": "track lost just past the line",
        })
    return out


def _broken_crossings(tracks: list[Track], geom: BoundGeometry, cands: list[dict[str, Any]],
                      duration: float) -> list[dict[str, Any]]:
    """A track lost just short of the line, then one appearing just past it: a likely miss."""
    h = geom.height
    near, reach = BROKEN_NEAR_FRAC * h, BROKEN_REACH_FRAC * h
    starts = sorted(tracks, key=lambda t: (t.start_t, t.track_id))
    start_times = [t.start_t for t in starts]
    used: set[int] = set()
    out: list[dict[str, Any]] = []
    for a in sorted(tracks, key=lambda t: (t.end_t, t.track_id)):
        if a.ended_by != LOST:
            continue
        ea = a.samples[-1]
        pa = np.array([ea.x, ea.y])
        if geo.distance_to_polyline(geom.line, pa) > near:
            continue
        side_a = geo.side_of_polyline(geom.line, pa)
        if side_a == 0:
            continue
        lo = bisect.bisect_right(start_times, ea.t)
        hi = bisect.bisect_right(start_times, ea.t + BROKEN_GAP_S)
        best: tuple[tuple[float, float], Track] | None = None
        for b in starts[lo:hi]:
            if b.track_id == a.track_id or b.track_id in used:
                continue
            fb = b.samples[0]
            pb = np.array([fb.x, fb.y])
            if (geo.side_of_polyline(geom.line, pb) != -side_a
                    or geo.distance_to_polyline(geom.line, pb) > near):
                continue
            dist = float(np.hypot(*(pb - pa)))
            if dist > reach:
                continue
            key = (fb.t - ea.t, dist)
            if best is None or key < best[0]:
                best = (key, b)
        if best is None:
            continue
        b = best[1]
        used.add(b.track_id)
        direction = IN if -side_a == geom.inside_sign else OUT
        t_mid = (ea.t + b.start_t) / 2
        if any(c["direction"] == direction and abs(c["t_seconds"] - t_mid) <= 2.0 for c in cands):
            continue
        out.append({
            "id": "",
            "start_s": round(max(0.0, ea.t - CLIP_PAD_S), 2),
            "end_s": round(min(duration, b.start_t + CLIP_PAD_S), 2),
            "kind": MISS_KIND,
            "direction_guess": direction,
            "t_seconds": round(t_mid, 2),
            "tracks": [a.track_id, b.track_id],
            "detail": "track lost at the line, another found just past it",
        })
    return out


def _dedupe_misses(misses: list[dict[str, Any]],
                   cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One likely miss per moment and direction, and none where a proposal already is."""
    kept: list[dict[str, Any]] = []
    for m in sorted(misses, key=lambda m: (m["t_seconds"], m["tracks"])):
        clash = [x for x in kept + cands
                 if x.get("direction_guess", x.get("direction")) == m["direction_guess"]
                 and abs(x["t_seconds"] - m["t_seconds"]) <= 2.0]
        if not clash:
            kept.append(m)
    return kept


def build_outputs(
    cfg: SiteConfig,
    geom: BoundGeometry,
    act: dict[str, Any],
    tracks: list[Track],
    det_times: list[float],
    det_counts: list[int],
    joins: int,
    splits: int,
    frame_dt: float,
    meta: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    duration = float(act["processed_duration_s"])
    rp = RuleParams.from_config(cfg, geom, frame_dt)
    by_id = {t.track_id: t for t in tracks}
    spans = [(t.start_t, t.end_t) for t in tracks]
    committed: list[Committed] = []
    rejected: list[Rejected] = []
    for t in tracks:
        o = evaluate_track(t, rp)
        committed.extend(o.committed)
        rejected.extend(o.rejected)

    def window(lo: float, hi: float, anchor: float) -> tuple[float, float]:
        lo, hi = max(0.0, lo), min(duration, hi)
        if hi - lo > CLIP_MAX_S:
            lo = max(lo, anchor - CLIP_MAX_S / 2)
            hi = min(hi, lo + CLIP_MAX_S)
        return round(lo, 2), round(hi, 2)

    def path(tr: Track, lo: float, hi: float) -> list[list[float]]:
        return [[round(s.t, 2), round(s.x, 1), round(s.y, 1)] for s in tr.samples
                if lo <= s.t <= hi]

    def concurrent(t: float) -> int:
        w = CONCURRENCY_WINDOW_S
        return sum(1 for s, e in spans if s <= t + w and e >= t - w)

    def big_jump(tr: Track, t: float) -> bool:
        near = [s for s in tr.samples if abs(s.t - t) <= BIG_JUMP_WINDOW_S]
        limit = BIG_JUMP_FRAC * geom.height
        return any(np.hypot(b.x - a.x, b.y - a.y) > limit for a, b in itertools.pairwise(near))

    slug = slugify(cfg.sensor)
    cands: list[dict[str, Any]] = []
    for c in sorted(committed, key=lambda c: (c.crossing.t, c.track_id)):
        tr = by_id[c.track_id]
        x = c.crossing
        near = min(tr.samples, key=lambda s: abs(s.t - x.t))
        confs = [s.conf for s in tr.samples if abs(s.t - x.t) <= 1.0] or [near.conf]
        lo, hi = x.t - CLIP_PAD_S, max(x.t, c.commit_t) + CLIP_PAD_S
        if c.dwell_start is not None:
            lo = min(lo, c.dwell_start - 0.5)
            hi = max(hi, c.dwell_start + cfg.min_dwell_in_zone_s + 0.5)
        cs, ce = window(lo, hi, x.t)
        cands.append({
            "id": "",
            "track_id": c.track_id,
            "t_seconds": round(x.t, 3),
            "direction": x.direction,
            "confidence": round(float(np.mean(confs)), 3),
            "bbox": [round(v, 1) for v in near.bbox],
            "clip_start": cs,
            "clip_end": ce,
            "line_position": round(x.line_position, 4),
            "concurrent_tracks": concurrent(x.t),
            "path": path(tr, cs, ce),
            "crossing_xy": [round(x.x, 1), round(x.y, 1)],
            "commit_t": round(c.commit_t, 3),
            "dwell_s": c.dwell_s,
            "flags": list(c.flags) + ([FLAG_BIG_JUMP] if big_jump(tr, x.t) else []),
            "stitched_from": list(tr.stitched_from),
        })
    cands, dups = _merge_duplicates(cands)
    for i, cand in enumerate(cands):
        cand["id"] = f"{slug}-{i + 1:04d}"

    # Opposite-direction counts close in time and place on different tracks: maybe one
    # person returning whose track broke. Flag both for the reviewer.
    n_pairs = 0
    for i, a in enumerate(cands):
        for b in cands[i + 1:]:
            if b["t_seconds"] - a["t_seconds"] > RETURN_PAIR_WINDOW_S:
                break
            if (a["direction"] != b["direction"] and a["track_id"] != b["track_id"]
                    and abs(a["line_position"] - b["line_position"]) <= RETURN_PAIR_POSITION):
                for one, other in ((a, b), (b, a)):
                    if FLAG_POSSIBLE_RETURN not in one["flags"]:
                        one["flags"].append(FLAG_POSSIBLE_RETURN)
                    one.setdefault("paired_with", []).append(other["id"])
                n_pairs += 1

    discards: list[dict[str, Any]] = []
    for r in rejected:
        tr = by_id[r.track_id]
        t0, t1 = r.crossings[0].t, r.crossings[-1].t
        cs, ce = window(t0 - CLIP_PAD_S, t1 + CLIP_PAD_S + 2.0, t0)
        discards.append({
            "id": "",
            "track_id": r.track_id,
            "reason": r.reason,
            "pattern": r.pattern,
            "t_seconds": round(t0, 3),
            "crossings": [{"t": round(x.t, 3), "direction": x.direction,
                           "line_position": round(x.line_position, 4)} for x in r.crossings],
            "clip_start": cs,
            "clip_end": ce,
            "path": path(tr, cs, ce),
            "flags": list(r.flags),
            "stitched_from": list(tr.stitched_from),
        })
    for dup, winner in dups:
        discards.append({
            "id": "",
            "track_id": dup["track_id"],
            "reason": DUPLICATE,
            "pattern": None,
            "t_seconds": dup["t_seconds"],
            "crossings": [{"t": dup["t_seconds"], "direction": dup["direction"],
                           "line_position": dup["line_position"]}],
            "clip_start": dup["clip_start"],
            "clip_end": dup["clip_end"],
            "path": dup["path"],
            "flags": dup["flags"],
            "stitched_from": dup["stitched_from"],
            "duplicate_of": winner["id"],
        })
    discards.sort(key=lambda d: (d["t_seconds"], d["track_id"], d["reason"]))
    for i, d in enumerate(discards):
        d["id"] = f"{slug}-d{i + 1:04d}"

    # Unexplained: first the likely misses (a track broken at the line), then any motion
    # near the line that no proposal, discard or likely miss accounts for.
    with_outcome = {c.track_id for c in committed} | {r.track_id for r in rejected}
    misses = _dedupe_misses(
        _lost_at_line(tracks, rp, with_outcome, duration)
        + _broken_crossings(tracks, geom, cands, duration), cands)
    motion = [(m["start_s"], m["end_s"]) for m in act["motion_runs"]]
    cover = [(c["clip_start"], c["clip_end"]) for c in cands] + \
            [(d["clip_start"], d["clip_end"]) for d in discards] + \
            [(m["start_s"], m["end_s"]) for m in misses]
    dt_arr = np.asarray(det_times)
    dc_arr = np.asarray(det_counts)
    leftovers: list[dict[str, Any]] = []
    for s, e in _subtract(motion, cover):
        if e - s < 0.5:
            continue
        sel = (dt_arr >= s) & (dt_arr <= e)
        peak = int(dc_arr[sel].max()) if sel.any() else 0
        seen = sum(1 for ts, te in spans if ts <= e and te >= s)
        leftovers.append({
            "id": "",
            "start_s": round(s, 2),
            "end_s": round(e, 2),
            "kind": "no_person_detected" if peak == 0 else "people_seen_no_crossing",
            "max_people_detected": peak,
            "tracks_seen": seen,
        })
    unexplained = sorted(misses, key=lambda u: u["start_s"]) + leftovers
    for i, u in enumerate(unexplained):
        u["id"] = f"{slug}-u{i + 1:04d}"

    by_reason = Counter(d["reason"] for d in discards)
    n_in = sum(1 for c in cands if c["direction"] == IN)
    expired = by_reason.get(PENDING_EXPIRED, 0)
    rate = expired / len(cands) if cands else (float("inf") if expired else 0.0)
    summary = {
        "candidates": len(cands),
        "in": n_in,
        "out": len(cands) - n_in,
        "discarded": dict(sorted(by_reason.items())),
        "pending_expired": expired,
        "pending_expired_rate": None if rate == float("inf") else round(rate, 4),
        "pending_expired_alarm": bool(rate > EXPIRED_ALARM),
        "stitched_joins": joins,
        "track_splits": splits,
        "duplicates": len(dups),
        "possible_return_pairs": n_pairs,
        "flagged_exclusion_zone": sum(1 for c in cands if "exclusion_zone" in c["flags"]),
        "flagged_big_jump": sum(1 for c in cands if FLAG_BIG_JUMP in c["flags"]),
        "tracks": len(tracks),
        "frames_analysed": len(det_times),
        "mean_detections_per_frame": round(float(dc_arr.mean()), 3) if len(dc_arr) else 0.0,
        "likely_missed_crossings": len(misses),
        "unexplained_ranges": len(unexplained),
        "unexplained_s": round(sum(u["end_s"] - u["start_s"] for u in unexplained), 2),
        "unexplained_no_person": sum(1 for u in unexplained if u["kind"] == "no_person_detected"),
    }
    base = {"schema": SCHEMA, "tool_version": TOOL_VERSION, **meta, "summary": summary}
    return ({**base, "candidates": cands}, {**base, "discarded": discards},
            {**base, "unexplained": unexplained})


def run_detect(
    video_path: str | Path,
    config_paths: list[str | Path],
    run_dir: Path,
    *,
    opts: DetectOptions,
    sample_s: float | None = None,
    factory: DetectorFactory | None = None,
    progress: bool = False,
) -> list[CameraDetection]:
    info = vid.probe(video_path)
    audit = vid.audit_timebase(video_path)
    make = factory or yolo_factory(opts)
    median: NDArray[np.uint8] | None = None
    results: list[CameraDetection] = []
    for p in config_paths:
        cfg = load_config(p)
        act, act_sha = load_activity(run_dir, cfg, info, sample_s)
        t = act["picture"]["tile"]
        tile = Tile(t["index"], t["x0"], t["y0"], t["x1"], t["y1"], t["header_px"])
        geom = bind(cfg, tile.width, tile.height, tile.origin)
        region = tracking_region(geom, tile, (info.height, info.width))
        detector = make(cfg, geom, tile, region)
        n_static = 0
        if hasattr(detector, "learn_static"):
            if median is None:
                median = vid.median_of_video(video_path, float(act["processed_duration_s"]),
                                             interval_s=audit.median_interval_s)
            n_static = detector.learn_static(median)
        if progress:
            print(f"  {cfg.sensor}: {detector.name} detector, {n_static} static objects ignored",
                  file=sys.stderr)
        wall0 = time.monotonic()
        tr = track_camera(
            video_path, act, detector, audit.effective_fps, opts.det_fps, opts.batch_frames,
            cfg.stitch_gap_max_s, 0.08 * tile.height, JUMP_BASE_FRAC * tile.height,
            MAX_SPEED_FRAC * tile.height, progress, opts.tracker_kw())
        wall = time.monotonic() - wall0
        recorded = None
        if isinstance(detector, RecordingDetector):
            recorded = {"model": opts.model, "mode": detector.name, "frames": detector.frames}
        det_meta: dict[str, Any] = {"mode": detector.name, "model": opts.model, "conf": opts.conf,
                                    "det_fps": opts.det_fps, "tracker": opts.tracker_kw()}
        for attr in ("crop_px", "imgsz", "device"):
            if hasattr(detector, attr):
                det_meta[attr] = getattr(detector, attr)
        if hasattr(detector, "specs"):
            det_meta["crops_per_frame"] = len(detector.specs)
        det_meta["static_objects_ignored"] = n_static
        meta = {
            "video": {"filename": info.filename, "fingerprint": info.fingerprint},
            "config": {"path": cfg.source, "sha256": cfg.sha256, "site": cfg.site,
                       "sensor": cfg.sensor, "rule": cfg.rule},
            "activity_sha256": act_sha,
            "picture": act["picture"]["tile"],
            "detector": det_meta,
            "rule_params": {"min_dwell_in_zone_s": cfg.min_dwell_in_zone_s,
                            "pending_timeout_s": cfg.pending_timeout_s,
                            "stitch_gap_max_s": cfg.stitch_gap_max_s,
                            "same_track_returns": cfg.same_track_returns},
            "sample_s": sample_s,
            "processed_duration_s": act["processed_duration_s"],
        }
        cand, disc, unex = build_outputs(cfg, geom, act, tr.tracks, tr.det_times, tr.det_counts,
                                         tr.joins, tr.splits, tr.frame_dt, meta)
        results.append(CameraDetection(cfg, cand, disc, unex, tr.tracks, wall, recorded))
    return results


def output_dir(run_dir: Path, sensor: str, mode: str) -> Path:
    d = camera_dir(run_dir, sensor)
    return d if mode == "derotated" else d / mode


def write_detection(res: CameraDetection, run_dir: Path, mode: str) -> Path:
    d = output_dir(run_dir, res.cfg.sensor, mode)
    write_json_atomic(d / "candidates.json", res.candidates)
    write_json_atomic(d / "discarded.json", res.discarded)
    write_json_atomic(d / "unexplained.json", res.unexplained)
    write_json_atomic(d / "tracks.json", res.tracks_json())
    if res.recorded is not None:
        (d / "detections.pkl").write_bytes(pickle.dumps(res.recorded))
    return d


def fmt_summary(res: CameraDetection) -> list[str]:
    s = res.candidates["summary"]
    dur = res.candidates["processed_duration_s"]
    lines = [
        (f"  candidates: {s['candidates']}  (in {s['in']}, out {s['out']})   "
         f"tracks: {s['tracks']}   stitched joins: {s['stitched_joins']}   "
         f"split at jumps: {s['track_splits']}   duplicates merged: {s['duplicates']}"),
        "  discarded:  " + (", ".join(f"{k} {v}" for k, v in s["discarded"].items()) or "none"),
        (f"  likely missed crossings (track lost at the line): {s['likely_missed_crossings']}"),
        (f"  unexplained motion: {s['unexplained_ranges']} ranges, "
         f"{fmt_hms(s['unexplained_s'])} ({s['unexplained_no_person']} with nobody detected)"),
        (f"  possible returns (paired opposite counts): {s['possible_return_pairs']}   "
         f"exclusion-zone flags: {s['flagged_exclusion_zone']}   "
         f"big jump near the line: {s['flagged_big_jump']}"),
        (f"  {s['frames_analysed']} frames, {s['mean_detections_per_frame']:.2f} people/frame, "
         f"{res.wall_s:.0f} s ({dur / max(res.wall_s, 1e-9):.1f}x realtime over the whole clip)"),
    ]
    rate = s["pending_expired_rate"]
    if s["pending_expired_alarm"]:
        pct = "all" if rate is None else f"{100 * rate:.1f}%"
        lines.append(
            f"  *** WARNING: {s['pending_expired']} pending tracks expired without reaching the "
            f"mask zone = {pct} of committed counts (alarm above {100 * EXPIRED_ALARM:.0f}%). "
            f"The mask rule may be discarding real entries (occlusion between line and zone). "
            f"Review discarded.json reason=pending_expired. ***")
    return lines
