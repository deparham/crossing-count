"""Video access. Frame times always come from container timestamps (PTS).

CCTV exports frequently carry wrong frame-rate metadata or have been re-encoded
at a different rate than the recorder used, so `frame_index / fps` is never
trusted. `fps_assumed` from the site config is only a cross-check.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import av
import cv2
import numpy as np
from numpy.typing import NDArray

Image = NDArray[np.uint8]


@dataclass(frozen=True)
class Frame:
    index: int  # decode index from the start of the stream
    t: float  # seconds from the first frame, from PTS
    image: Image  # BGR


@dataclass(frozen=True)
class VideoInfo:
    path: str
    filename: str
    width: int
    height: int
    codec: str
    fps_reported: float
    size_bytes: int
    fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FilenameInterval:
    """A recording interval parsed from an NVR export filename."""

    start: str  # ISO local time, no zone
    end: str
    tz: str
    seconds: float


@dataclass
class TimebaseAudit:
    n_frames: int
    first_pts_s: float
    duration_s: float
    median_interval_s: float
    max_interval_s: float
    effective_fps: float
    fps_reported: float
    fps_assumed: float | None
    duplicate_pts: int
    gaps: list[tuple[float, float]]  # (video second, gap length) for gaps > 1 s
    filename_interval: FilenameInterval | None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["gaps"] = [{"at_s": round(a, 3), "length_s": round(b, 3)} for a, b in self.gaps]
        for k in ("first_pts_s", "duration_s", "median_interval_s", "max_interval_s"):
            d[k] = round(d[k], 4)
        d["effective_fps"] = round(self.effective_fps, 4)
        return d


def fingerprint(path: str | Path, chunk: int = 4 << 20) -> str:
    """Cheap content identity: size plus the first and last 4 MiB."""
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with p.open("rb") as f:
        h.update(f.read(chunk))
        if size > chunk:
            f.seek(max(chunk, size - chunk))
            h.update(f.read(chunk))
    return h.hexdigest()[:16]


def probe(path: str | Path) -> VideoInfo:
    p = Path(path)
    with av.open(str(p)) as c:
        s = c.streams.video[0]
        cc = s.codec_context
        rate = s.average_rate or s.base_rate
        return VideoInfo(
            path=str(p.resolve()),
            filename=p.name,
            width=int(cc.width),
            height=int(cc.height),
            codec=str(cc.name),
            fps_reported=float(rate) if rate else 0.0,
            size_bytes=p.stat().st_size,
            fingerprint=fingerprint(p),
        )


_INTERVAL_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})-(\d{2})(\d{2})(\d{2})\s*([A-Za-z]{2,5})?\s+to\s+"
    r"(\d{4}-\d{2}-\d{2})-(\d{2})(\d{2})(\d{2})"
)


def parse_filename_interval(name: str) -> FilenameInterval | None:
    """Parse e.g. '... 2026-07-16-120000 AWST to 2026-07-16-130000 AWST.mp4'."""
    m = _INTERVAL_RE.search(name)
    if not m:
        return None
    start = datetime.fromisoformat(f"{m[1]}T{m[2]}:{m[3]}:{m[4]}")
    end = datetime.fromisoformat(f"{m[6]}T{m[7]}:{m[8]}:{m[9]}")
    seconds = (end - start).total_seconds()
    if seconds <= 0:
        return None
    return FilenameInterval(start.isoformat(), end.isoformat(), m[5] or "", seconds)


def fps_mismatch_warning(effective_fps: float, fps_assumed: float) -> str | None:
    if abs(effective_fps - fps_assumed) / fps_assumed <= 0.005:
        return None
    return (
        f"fps_assumed={fps_assumed:g} but the footage runs at {effective_fps:.3f} fps. "
        f"All times use frame timestamps, so results are unaffected, but "
        f"'frame / {fps_assumed:g}' would be wrong by {effective_fps / fps_assumed:.2f}x. "
        f"Fix fps_assumed in the site config."
    )


def audit_timebase(path: str | Path, fps_assumed: float | None = None) -> TimebaseAudit:
    """Scan packet timestamps (no decoding) and report anything that breaks timing."""
    p = Path(path)
    with av.open(str(p)) as c:
        s = c.streams.video[0]
        tb = _time_base(s)
        rate = s.average_rate or s.base_rate
        fps_reported = float(rate) if rate else 0.0
        pts = np.array(
            sorted(pkt.pts for pkt in c.demux(s) if pkt.pts is not None), dtype=np.float64
        )
    if len(pts) < 2:
        raise ValueError(f"{p.name}: fewer than two timestamped video frames")
    pts *= tb
    d = np.diff(pts)
    duplicates = int((d == 0).sum())
    pos = d[d > 0]
    median = float(np.median(pos))
    duration = float(pts[-1] - pts[0] + median)
    effective_fps = 1.0 / median
    gaps = [(float(pts[i] - pts[0]), float(d[i])) for i in np.nonzero(d > 1.0)[0]]

    audit = TimebaseAudit(
        n_frames=len(pts),
        first_pts_s=float(pts[0]),
        duration_s=duration,
        median_interval_s=median,
        max_interval_s=float(d.max()),
        effective_fps=effective_fps,
        fps_reported=fps_reported,
        fps_assumed=fps_assumed,
        duplicate_pts=duplicates,
        gaps=gaps,
        filename_interval=parse_filename_interval(p.name),
    )

    if fps_assumed is not None:
        w = fps_mismatch_warning(effective_fps, fps_assumed)
        if w:
            audit.warnings.append(w)
    if gaps:
        total = sum(g for _, g in gaps)
        audit.warnings.append(
            f"{len(gaps)} timestamp gap(s) over 1 s totalling {total:.1f} s; the recorder "
            f"was not continuous. Video time will not map linearly to wall-clock time."
        )
    if duplicates:
        audit.warnings.append(f"{duplicates} frame(s) share a timestamp with their predecessor.")
    fi = audit.filename_interval
    if fi is not None and abs(fi.seconds - duration) > 1.0:
        audit.warnings.append(
            f"filename says {fi.start} to {fi.end} ({fi.seconds:.0f} s) but the video spans "
            f"{duration:.1f} s ({duration - fi.seconds:+.1f} s). Timestamps are regular, so "
            f"frames were dropped before export and re-timed evenly: video time will drift "
            f"from wall-clock time by up to {abs(fi.seconds - duration):.1f} s."
        )
    return audit


def _time_base(stream: Any) -> float:
    if stream.time_base is None:
        raise ValueError("video stream has no time base; cannot read frame timestamps")
    return float(stream.time_base)


def _open_video(path: str | Path) -> tuple[Any, Any, float, int]:
    c = av.open(str(path))
    s = c.streams.video[0]
    s.thread_type = "AUTO"
    tb = _time_base(s)
    start = s.start_time if s.start_time is not None else 0
    return c, s, tb, start


def _to_image(frame: Any, size: tuple[int, int] | None) -> Image:
    img = np.ascontiguousarray(frame.to_ndarray(format="bgr24"), dtype=np.uint8)
    if size is None or (img.shape[1], img.shape[0]) == size:
        return img
    # Always resize with OpenCV so every stage sees identical pixels for a given size.
    return np.asarray(cv2.resize(img, size, interpolation=cv2.INTER_AREA), dtype=np.uint8)


def iter_frames(
    path: str | Path,
    *,
    end_s: float | None = None,
    stride: int = 1,
    size: tuple[int, int] | None = None,
) -> Iterator[Frame]:
    """Decode frames from the start, yielding every `stride`-th one before `end_s`."""
    c, s, tb, start = _open_video(path)
    try:
        last_t = -1.0
        for i, fr in enumerate(c.decode(s)):
            if fr.pts is not None:
                t = (fr.pts - start) * tb
            else:  # untimed frame: nudge forward so time stays monotonic
                t = last_t + 1e-3
            last_t = t
            if end_s is not None and t >= end_s:
                break
            if i % stride:
                continue
            yield Frame(i, t, _to_image(fr, size))
    finally:
        c.close()


def iter_range(
    path: str | Path,
    start_s: float,
    end_s: float,
    *,
    stride: int = 1,
    size: tuple[int, int] | None = None,
) -> Iterator[Frame]:
    """Every `stride`-th frame with start_s <= t < end_s (seeks to the keyframe before)."""
    c, s, tb, start = _open_video(path)
    try:
        c.seek(int(start_s / tb) + start, stream=s, backward=True, any_frame=False)
        k = 0
        for fr in c.decode(s):
            if fr.pts is None:
                continue
            t = (fr.pts - start) * tb
            if t < start_s - 1e-6:
                continue
            if t >= end_s:
                break
            if k % stride == 0:
                yield Frame(-1, t, _to_image(fr, size))
            k += 1
    finally:
        c.close()


def median_of_video(path: str | Path, duration_s: float, n: int = 31, span_s: float = 600.0,
                    interval_s: float = 0.1) -> Image:
    """Per-pixel median of `n` frames spread over the first `span_s`: a people-free picture."""
    span = max(0.0, min(duration_s, span_s) - 2 * interval_s)
    frames = grab_frames_at(path, np.linspace(0.0, span, n).tolist())
    return np.asarray(np.median(np.stack([f.image for f in frames]), axis=0),
                      dtype=np.float64).astype(np.uint8)


def grab_frames_at(
    path: str | Path, times: Sequence[float], size: tuple[int, int] | None = None
) -> list[Frame]:
    """Return the frame at or just after each requested time (seek + decode forward)."""
    out: list[Frame] = []
    c, s, tb, start = _open_video(path)
    try:
        for target in sorted(times):
            c.seek(int(target / tb) + start, stream=s, backward=True, any_frame=False)
            for fr in c.decode(s):
                if fr.pts is None:
                    continue
                t = (fr.pts - start) * tb
                if t >= target - tb:
                    out.append(Frame(-1, t, _to_image(fr, size)))
                    break
    finally:
        c.close()
    return out
