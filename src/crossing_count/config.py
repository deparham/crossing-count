"""Per-camera site configuration: loading, validation, and binding to pixel space.

Configs are produced by trace_line.py, never hand-edited. Coordinates are
normalised 0-1 relative to the camera's own picture (its tile), so a config
survives resolution changes and different multi-camera layouts. Loading is
strict: unknown keys are rejected so a typo cannot silently become a default.

What a camera has decides its counting rule (see rule.py):
  line only             a crossing counts
  line + mask_zone      the crossing's track must dwell in the mask zone
  line + filter_zones   the track must touch a filter zone somewhere on its path
  line + both           both conditions
In every case a person who crosses and comes back on the same track is not
counted and is reported instead, unless same_track_returns is "count".
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import numpy as np

from . import geometry as geo
from .geometry import FloatArray

NormPoint = tuple[float, float]
NormPoly = tuple[NormPoint, ...]
HSV = tuple[int, int, int]


class ConfigError(ValueError):
    """The site config is malformed or geometrically inconsistent."""


@dataclass(frozen=True)
class GateParams:
    """Motion-gate tuning. Every default leans toward keeping time, not cutting it."""

    line_margin: float = 0.10  # band radius around the line, fraction of picture height
    zone_margin: float = 0.03  # dilation of the mask zone, fraction of picture height
    warmup_s: float = 2.0  # always kept while noise statistics settle
    absorb_s: float = 60.0  # a motionless person stays foreground this long
    var_threshold: float = 16.0  # MOG2 squared-Mahalanobis threshold
    min_blob_frac: float = 0.0003  # smallest foreground blob, fraction of picture area
    pre_roll_s: float = 3.0
    post_roll_s: float = 3.0
    merge_gap_s: float = 5.0
    proc_fps: float = 10.0  # frames per second actually analysed
    proc_width: int = 640  # pictures wider than this are downscaled for analysis

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> GateParams:
        defaults = cls()
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ConfigError(f"unknown gate parameter(s): {unknown}")
        coerced: dict[str, Any] = {}
        for key, value in raw.items():
            kind = type(getattr(defaults, key))
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"gate.{key} must be a number, got {value!r}")
            coerced[key] = kind(value)
        params = replace(defaults, **coerced)
        params.validate()
        return params

    def with_overrides(self, **overrides: Any) -> GateParams:
        clean = {k: v for k, v in overrides.items() if v is not None}
        return GateParams.from_dict({**self.as_dict(), **clean}) if clean else self

    def as_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def validate(self) -> None:
        for name, value in self.as_dict().items():
            if value < 0:
                raise ConfigError(f"gate.{name} must be >= 0, got {value}")
        if self.proc_fps <= 0:
            raise ConfigError("gate.proc_fps must be > 0")
        if self.proc_width < 64:
            raise ConfigError("gate.proc_width must be >= 64")
        if self.line_margin <= 0:
            raise ConfigError("gate.line_margin must be > 0")


_REQUIRED = ("site", "sensor", "line", "inside_side", "fps_assumed")
_OPTIONAL = (
    "mask_zone",
    "min_dwell_in_zone_s",
    "pending_timeout_s",
    "stitch_gap_max_s",
    "filter_zones",
    "exclusion_zones",
    "gate_ignore_zones",
    "overlay_hsv",
    "traced_on",
    "notes",
    "gate",
    "same_track_returns",
    "fisheye_center",
)
_RETURNS = ("flag", "count")
_SIDE_WORDS = ("below", "above", "left", "right")


@dataclass(frozen=True)
class SiteConfig:
    site: str
    sensor: str
    line: NormPoly
    inside_side: str
    fps_assumed: float
    mask_zone: NormPoly | None = None
    min_dwell_in_zone_s: float = 0.4
    pending_timeout_s: float = 20.0
    stitch_gap_max_s: float = 1.5
    filter_zones: tuple[NormPoly, ...] = ()
    exclusion_zones: tuple[NormPoly, ...] = ()
    gate_ignore_zones: tuple[NormPoly, ...] = ()
    overlay_hsv: HSV | None = None
    traced_on: dict[str, Any] | None = None
    notes: str = ""
    gate: GateParams = field(default_factory=GateParams)
    same_track_returns: str = "flag"
    fisheye_center: NormPoint = (0.5, 0.5)
    source: str = "<dict>"
    sha256: str = ""

    @property
    def rule(self) -> str:
        parts = ["line"]
        if self.mask_zone is not None:
            parts.append("mask")
        if self.filter_zones:
            parts.append("filter")
        return "+".join(parts)

    def to_json_dict(self) -> dict[str, Any]:
        """The on-disk form. Gate parameters are written only where they differ."""

        def poly(z: NormPoly) -> list[list[float]]:
            return [list(p) for p in z]

        out: dict[str, Any] = {
            "site": self.site,
            "sensor": self.sensor,
            "line": poly(self.line),
            "inside_side": self.inside_side,
        }
        if self.mask_zone is not None:
            out["mask_zone"] = poly(self.mask_zone)
        out["min_dwell_in_zone_s"] = self.min_dwell_in_zone_s
        out["pending_timeout_s"] = self.pending_timeout_s
        out["stitch_gap_max_s"] = self.stitch_gap_max_s
        out["filter_zones"] = [poly(z) for z in self.filter_zones]
        out["exclusion_zones"] = [poly(z) for z in self.exclusion_zones]
        if self.gate_ignore_zones:
            out["gate_ignore_zones"] = [poly(z) for z in self.gate_ignore_zones]
        out["fps_assumed"] = self.fps_assumed
        if self.overlay_hsv is not None:
            out["overlay_hsv"] = list(self.overlay_hsv)
        if self.traced_on is not None:
            out["traced_on"] = self.traced_on
        out["notes"] = self.notes
        if self.same_track_returns != "flag":
            out["same_track_returns"] = self.same_track_returns
        if self.fisheye_center != (0.5, 0.5):
            out["fisheye_center"] = list(self.fisheye_center)
        defaults = GateParams().as_dict()
        overrides = {k: v for k, v in self.gate.as_dict().items() if defaults[k] != v}
        if overrides:
            out["gate"] = overrides
        return out


def _points(value: Any, name: str, min_points: int) -> NormPoly:
    if not isinstance(value, list) or len(value) < min_points:
        raise ConfigError(f"{name} must be a list of at least {min_points} [x, y] points")
    pts: list[NormPoint] = []
    for i, p in enumerate(value):
        if (
            not isinstance(p, list)
            or len(p) != 2
            or not all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in p)
        ):
            raise ConfigError(f"{name}[{i}] must be an [x, y] pair of numbers, got {p!r}")
        x, y = float(p[0]), float(p[1])
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ConfigError(f"{name}[{i}] = {p!r} is outside the normalised 0-1 range")
        pts.append((x, y))
    for i in range(len(pts) - 1):
        if pts[i] == pts[i + 1]:
            raise ConfigError(f"{name} has a repeated vertex at index {i}")
    return tuple(pts)


def _polygons(value: Any, name: str) -> tuple[NormPoly, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{name} must be a list of polygons")
    return tuple(_points(z, f"{name}[{i}]", 3) for i, z in enumerate(value))


def _positive(raw: dict[str, Any], key: str, default: float, allow_zero: bool = False) -> float:
    v = raw.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or (v == 0 and not allow_zero):
        kind = "a number >= 0" if allow_zero else "a positive number"
        raise ConfigError(f"{key} must be {kind}, got {v!r}")
    return float(v)


def parse_config(raw: dict[str, Any], source: str = "<dict>", sha256: str = "") -> SiteConfig:
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    missing = [k for k in _REQUIRED if k not in raw]
    if missing:
        raise ConfigError(f"config is missing required key(s): {missing}")
    unknown = sorted(set(raw) - set(_REQUIRED) - set(_OPTIONAL))
    if unknown:
        raise ConfigError(f"config has unknown key(s): {unknown}")
    for key in ("site", "sensor", "inside_side"):
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise ConfigError(f"{key} must be a non-empty string")
    inside = raw["inside_side"].strip().lower()
    if inside not in _SIDE_WORDS:
        raise ConfigError(f"inside_side must be one of {list(_SIDE_WORDS)}, got {inside!r}")

    mask_raw = raw.get("mask_zone")
    mask_zone = None if mask_raw is None else _points(mask_raw, "mask_zone", 3)

    hsv_raw = raw.get("overlay_hsv")
    overlay_hsv: HSV | None = None
    if hsv_raw is not None:
        if (
            not isinstance(hsv_raw, list)
            or len(hsv_raw) != 3
            or not all(isinstance(c, int) and not isinstance(c, bool) for c in hsv_raw)
        ):
            raise ConfigError("overlay_hsv must be [h, s, v] integers")
        overlay_hsv = (hsv_raw[0], hsv_raw[1], hsv_raw[2])

    traced_on = raw.get("traced_on")
    if traced_on is not None and not isinstance(traced_on, dict):
        raise ConfigError("traced_on must be an object")
    gate_raw = raw.get("gate", {})
    if not isinstance(gate_raw, dict):
        raise ConfigError("gate must be an object of parameter overrides")
    notes = raw.get("notes", "")
    if not isinstance(notes, str):
        raise ConfigError("notes must be a string")
    returns = raw.get("same_track_returns", "flag")
    if returns not in _RETURNS:
        raise ConfigError(f"same_track_returns must be one of {list(_RETURNS)}, got {returns!r}")
    fisheye_center = _points([raw.get("fisheye_center", [0.5, 0.5])], "fisheye_center", 1)[0]

    return SiteConfig(
        site=raw["site"],
        sensor=raw["sensor"],
        line=_points(raw["line"], "line", 2),
        inside_side=inside,
        fps_assumed=_positive(raw, "fps_assumed", 0),
        mask_zone=mask_zone,
        min_dwell_in_zone_s=_positive(raw, "min_dwell_in_zone_s", 0.4, allow_zero=True),
        pending_timeout_s=_positive(raw, "pending_timeout_s", 20.0),
        stitch_gap_max_s=_positive(raw, "stitch_gap_max_s", 1.5),
        filter_zones=_polygons(raw.get("filter_zones", []), "filter_zones"),
        exclusion_zones=_polygons(raw.get("exclusion_zones", []), "exclusion_zones"),
        gate_ignore_zones=_polygons(raw.get("gate_ignore_zones", []), "gate_ignore_zones"),
        overlay_hsv=overlay_hsv,
        traced_on=traced_on,
        notes=notes,
        gate=GateParams.from_dict(gate_raw),
        same_track_returns=returns,
        fisheye_center=fisheye_center,
        source=source,
        sha256=sha256,
    )


def load_config(path: str | Path) -> SiteConfig:
    p = Path(path)
    data = p.read_bytes()
    try:
        raw = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{p}: invalid JSON: {exc}") from exc
    return parse_config(raw, source=str(p), sha256=hashlib.sha256(data).hexdigest())


@dataclass(frozen=True)
class BoundGeometry:
    """A config's geometry in frame pixels, for a picture of a given size and origin."""

    width: int  # picture (tile) size
    height: int
    origin: tuple[float, float]
    line: FloatArray
    inside_sign: int
    mask_zone: FloatArray | None
    filter_zones: tuple[FloatArray, ...]
    exclusion_zones: tuple[FloatArray, ...]
    gate_ignore_zones: tuple[FloatArray, ...]
    warnings: tuple[str, ...]
    fisheye_center: FloatArray = field(default_factory=lambda: np.zeros(2))


def mask_depth_px(poly: FloatArray) -> float:
    """Mean depth of a band-like polygon: 2 * area / perimeter (exact for a thin rectangle)."""
    pts = np.asarray(poly, dtype=np.float64)
    x, y = pts[:, 0], pts[:, 1]
    area = abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) / 2
    perimeter = float(np.hypot(*(np.roll(pts, -1, axis=0) - pts).T).sum())
    return 2 * area / perimeter if perimeter > 0 else 0.0


def aspect_matches(cfg: SiteConfig, width: int, height: int) -> bool:
    """False if the picture's shape differs from the one the config was traced on."""
    size = (cfg.traced_on or {}).get("tile_size")
    if not isinstance(size, list) or len(size) != 2:
        return True
    tw, th = size
    return bool(abs((width / height) / (tw / th) - 1.0) <= 0.02)


def bind(
    cfg: SiteConfig, width: int, height: int, origin: tuple[float, float] = (0.0, 0.0)
) -> BoundGeometry:
    """Map normalised geometry into a picture of `width` x `height` at `origin`.

    Raises ConfigError if the picture's aspect ratio differs from the one the
    config was traced on, or if the mask zone is not on the inside of the line.
    """
    if not aspect_matches(cfg, width, height):
        tw, th = (cfg.traced_on or {})["tile_size"]
        raise ConfigError(
            f"{cfg.sensor} was traced on a {tw}x{th} picture but this picture is "
            f"{width}x{height} (different shape); retrace it for this layout"
        )

    ox, oy = origin

    def px(points: Any) -> FloatArray:
        return geo.to_pixels(points, width, height) + np.array([ox, oy], dtype=np.float64)

    line = px(cfg.line)
    try:
        sign = geo.resolve_inside_sign(line, cfg.inside_side)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    warnings: list[str] = []
    zones = [("mask_zone", [cfg.mask_zone] if cfg.mask_zone is not None else []),
             ("filter_zones", list(cfg.filter_zones)), ("exclusion_zones", list(cfg.exclusion_zones)),
             ("gate_ignore_zones", list(cfg.gate_ignore_zones))]
    for name, polys in zones:
        for z in polys:
            if geo.polygon_self_intersects(px(z)):
                raise ConfigError(
                    f"{cfg.sensor}: a {name.rstrip('s').replace('_', ' ')}'s edges cross each other. "
                    f"Click its corners in order around the shape.")
    mask = px(cfg.mask_zone) if cfg.mask_zone is not None else None
    if mask is not None:
        if geo.side_of_polyline(line, geo.polygon_centroid(mask)) != sign:
            raise ConfigError(
                f"{cfg.sensor}: mask_zone's centre is not on the inside of the counting line "
                f"(inside_side={cfg.inside_side!r}). The mask zone must sit between the line "
                f"and the store interior; retrace it with trace_line.py."
            )
        depth = mask_depth_px(mask)
        if cfg.min_dwell_in_zone_s > 0 and depth < 0.05 * height:
            warnings.append(
                f"{cfg.sensor}: mask_zone is only about {depth:.0f} px deep, so someone walking "
                f"through spends a fraction of a second in it and min_dwell_in_zone_s="
                f"{cfg.min_dwell_in_zone_s:g} will reject real entries. For a thin mask band "
                f"(as RetailNext draws them) set min_dwell_in_zone_s to 0: passing through is "
                f"enough."
            )
        outside = [i for i, v in enumerate(mask) if geo.side_of_polyline(line, v) == -sign]
        if outside or geo.polyline_intersects_polygon(line, mask):
            warnings.append(
                f"{cfg.sensor}: mask_zone overlaps the counting line (vertices on the outside: "
                f"{outside}). Someone standing just outside the threshold could satisfy the "
                f"mask condition; consider retracing so the zone lies wholly inside."
            )

    return BoundGeometry(
        width=width,
        height=height,
        origin=(float(ox), float(oy)),
        line=line,
        inside_sign=sign,
        mask_zone=mask,
        filter_zones=tuple(px(z) for z in cfg.filter_zones),
        exclusion_zones=tuple(px(z) for z in cfg.exclusion_zones),
        gate_ignore_zones=tuple(px(z) for z in cfg.gate_ignore_zones),
        warnings=tuple(warnings),
        fisheye_center=px([cfg.fisheye_center])[0],
    )
