"""Local setup page: draw each camera's counting line and zones on its own picture.

Runs on 127.0.0.1 only. The video never leaves this machine: the page asks this
server for JPEG crops of it and nothing else, and the page loads nothing from
the internet. Configs are validated with the same code as trace_line.py and
written to sites/.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from numpy.typing import NDArray
from pydantic import BaseModel, Field

from . import geometry as geo
from . import layout as lay
from . import overlay as ov
from . import tracing
from . import video as vid
from .config import ConfigError, aspect_matches, bind, load_config, parse_config
from .util import slugify, write_json_atomic

WEB_DIR = Path(__file__).parent / "web"
Pt = list[float]


class Draft(BaseModel):
    """A camera config as drawn, in picture pixels."""

    picture: int
    site: str = ""
    sensor: str = ""
    line: list[Pt] = Field(default_factory=list)
    inside: Pt | None = None
    mask_zone: list[Pt] | None = None
    filter_zones: list[list[Pt]] = Field(default_factory=list)
    exclusion_zones: list[list[Pt]] = Field(default_factory=list)
    gate_ignore_zones: list[list[Pt]] = Field(default_factory=list)
    min_dwell_in_zone_s: float = 0.0
    pending_timeout_s: float = 20.0
    stitch_gap_max_s: float = 1.5
    same_track_returns: str = "flag"
    gate: dict[str, Any] | None = None
    overwrite: bool = False


def _pts(a: Any) -> list[Pt]:
    return [[round(float(x), 1), round(float(y), 1)] for x, y in a]


class Setup:
    def __init__(self, video: Path, sites_dir: Path) -> None:
        self.video = video
        self.sites_dir = sites_dir
        self.info = vid.probe(video)
        self.audit = vid.audit_timebase(video)
        self.median = vid.median_of_video(video, self.audit.duration_s,
                                          interval_s=self.audit.median_interval_s)
        self.tiles = lay.detect_tiles_in(self.median)
        self._frames: dict[float, NDArray[np.uint8]] = {}

    def tile(self, i: int) -> lay.Tile:
        if not 0 <= i < len(self.tiles):
            raise HTTPException(404, f"no camera picture #{i}")
        return self.tiles[i]

    def picture_jpeg(self, i: int, t: float | None) -> bytes:
        tile = self.tile(i)
        if t is None:
            img = self.median
        else:
            key = round(max(0.0, min(t, self.audit.duration_s - 0.2)), 1)
            if key not in self._frames:
                if len(self._frames) > 40:
                    self._frames.clear()
                self._frames[key] = vid.grab_frames_at(self.video, [key])[0].image
            img = self._frames[key]
        ok, buf = cv2.imencode(".jpg", tile.crop(img), [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not ok:
            raise HTTPException(500, "could not encode picture")
        return bytes(buf.tobytes())

    def _safe(self, path: str) -> Path:
        p = Path(path).resolve()
        if self.sites_dir.resolve() not in p.parents or p.suffix != ".json":
            raise HTTPException(400, "configs can only be loaded from the sites folder")
        return p

    def existing(self) -> list[dict[str, Any]]:
        """Configs in sites/, each matched to the picture whose burned-in line it fits.

        A config with a geometry problem is still listed (with the problem), so it
        can be opened here and fixed.
        """
        out: list[dict[str, Any]] = []
        for p in sorted(self.sites_dir.glob("*.json")):
            try:
                cfg = load_config(p)
            except (ConfigError, ValueError, KeyError):
                continue
            scores: dict[int, float] = {}
            if cfg.overlay_hsv is not None:
                for t in self.tiles:
                    if aspect_matches(cfg, t.width, t.height):
                        line = geo.to_pixels(cfg.line, t.width, t.height) + np.array(t.origin)
                        scores[t.index] = round(
                            ov.line_match_score(self.median, line, cfg.overlay_hsv), 3)
            best = max(scores, key=lambda k: scores[k]) if scores else None
            matched = best is not None and scores[best] >= 0.5
            # Drawn on this very video but no burned-in line to match (a clean export):
            # it belongs to the picture it was drawn on. Configs from other videos are not
            # placed without a line match, so another camera's drawing never shows up here.
            drawn_on = (cfg.traced_on or {}).get("tile_index")
            same_video = (cfg.traced_on or {}).get("video") == self.info.filename
            if (not matched and same_video and isinstance(drawn_on, int)
                    and 0 <= drawn_on < len(self.tiles)
                    and aspect_matches(cfg, self.tiles[drawn_on].width,
                                       self.tiles[drawn_on].height)):
                best, matched = drawn_on, True  # no burned-in line here: the picture drawn on
                scores.pop(best, None)
            size = self.tiles[best if best is not None else 0]
            problem = None
            try:
                bind(cfg, size.width, size.height)
            except ConfigError as exc:
                problem = str(exc)
            out.append({"path": str(p), "file": p.name, "sensor": cfg.sensor, "site": cfg.site,
                        "rule": cfg.rule, "picture": best if matched else None,
                        "match": scores.get(best) if matched and best is not None else None,
                        "problem": problem})
        return out

    def shapes_of(self, path: str, i: int) -> dict[str, Any]:
        """A saved config's shapes in picture pixels, even if its geometry needs fixing."""
        cfg = load_config(self._safe(path))
        tile = self.tile(i)
        if not aspect_matches(cfg, tile.width, tile.height):
            raise HTTPException(400, f"{cfg.sensor} was drawn on a differently shaped picture")
        w, h = tile.width, tile.height

        def px(pts: Any) -> list[Pt]:
            return _pts(geo.to_pixels(pts, w, h))

        line = px(cfg.line)
        try:
            sign = geo.resolve_inside_sign(np.asarray(line, dtype=np.float64), cfg.inside_side)
        except ValueError:
            sign = 1
        return {
            "site": cfg.site, "sensor": cfg.sensor, "line": line,
            "inside": list(tracing.inside_point_for([(p[0], p[1]) for p in line], sign)),
            "mask_zone": px(cfg.mask_zone) if cfg.mask_zone is not None else None,
            "filter_zones": [px(z) for z in cfg.filter_zones],
            "exclusion_zones": [px(z) for z in cfg.exclusion_zones],
            "gate_ignore_zones": [px(z) for z in cfg.gate_ignore_zones],
            "min_dwell_in_zone_s": cfg.min_dwell_in_zone_s,
            "pending_timeout_s": cfg.pending_timeout_s,
            "stitch_gap_max_s": cfg.stitch_gap_max_s,
            "same_track_returns": cfg.same_track_returns,
            "gate": cfg.to_json_dict().get("gate"),
        }

    def evaluate(self, d: Draft) -> dict[str, Any]:
        tile = self.tile(d.picture)
        errors: list[str] = []
        warnings: list[str] = []
        res: dict[str, Any] = {"ok": False, "errors": errors, "warnings": warnings,
                               "config": None, "rule": None, "inside_side": None,
                               "line_match": None}
        if not d.site.strip():
            errors.append("Enter the site name.")
        if not d.sensor.strip():
            errors.append("Enter the sensor (camera) name.")
        if len(d.line) < 2:
            errors.append("Draw the counting line (at least 2 points).")
        if d.inside is None:
            errors.append("Click the inside (store) side of the line.")
        if d.mask_zone is not None and len(d.mask_zone) < 3:
            errors.append("The mask zone needs at least 3 corners.")
        for name, zones in (("filter", d.filter_zones), ("exclusion", d.exclusion_zones),
                            ("gate-ignore", d.gate_ignore_zones)):
            if any(len(z) < 3 for z in zones):
                errors.append(f"Each {name} zone needs at least 3 corners.")
        if d.same_track_returns not in ("flag", "count"):
            errors.append("Returns must be 'flag' or 'count'.")

        crop = tile.crop(self.median)
        hsv = None
        if len(d.line) >= 2:
            line = np.asarray(d.line, dtype=np.float64)
            hsv = ov.overlay_color_from_line(crop, line)
            if hsv is not None:
                res["line_match"] = round(ov.line_match_score(crop, line, hsv), 3)
        if errors:
            return res
        assert d.inside is not None
        try:
            raw, warns = tracing.build_config_dict(
                site=d.site.strip(), sensor=d.sensor.strip(),
                tile_size=(tile.width, tile.height),
                line=[(p[0], p[1]) for p in d.line], inside_point=(d.inside[0], d.inside[1]),
                mask_zone=[(p[0], p[1]) for p in d.mask_zone] if d.mask_zone else None,
                filter_zones=[[(p[0], p[1]) for p in z] for z in d.filter_zones],
                exclusion_zones=[[(p[0], p[1]) for p in z] for z in d.exclusion_zones],
                gate_ignore_zones=[[(p[0], p[1]) for p in z] for z in d.gate_ignore_zones],
                fps=self.audit.effective_fps, overlay_hsv=hsv,
                traced_on={"video": self.info.filename, "tile_index": tile.index,
                           "tile_size": [tile.width, tile.height],
                           "date": datetime.now().astimezone().date().isoformat(),
                           "method": "setup page"},
                min_dwell_in_zone_s=d.min_dwell_in_zone_s, pending_timeout_s=d.pending_timeout_s,
                stitch_gap_max_s=d.stitch_gap_max_s,
                notes=f"drawn on '{self.info.filename}' picture #{tile.index}", gate=d.gate,
            )
        except ConfigError as exc:
            errors.append(str(exc))
            return res
        if d.same_track_returns != "flag":
            raw["same_track_returns"] = d.same_track_returns
        warnings.extend(warns)
        if hsv is None:
            warnings.append("No burned-in line colour found under the counting line. Draw it on "
                            "the light-blue line; otherwise multi-camera videos need --tile.")
        elif res["line_match"] is not None and res["line_match"] < 0.8:
            warnings.append(f"Only {res['line_match']:.0%} of your line sits on the burned-in "
                            f"line. Add points where it bends.")
        cfg = parse_config(raw)
        res.update(ok=True, config=raw, rule=cfg.rule, inside_side=cfg.inside_side)
        return res

    def save(self, d: Draft) -> dict[str, Any]:
        res = self.evaluate(d)
        if not res["ok"]:
            raise HTTPException(400, " ".join(res["errors"]))
        path = self.sites_dir / f"{slugify(d.sensor)}.json"
        if path.exists() and not d.overwrite:
            return {"exists": True, "path": str(path)}
        write_json_atomic(path, res["config"])
        return {"saved": True, "path": str(path), "warnings": res["warnings"]}


def create_app(video: str | Path, sites_dir: str | Path = "sites",
               setup: Setup | None = None) -> FastAPI:
    """The setup page for one video. Its URLs are relative, so it also works mounted
    under a prefix (the count wizard shows it at /draw/<id>/)."""
    s = setup or Setup(Path(video), Path(sites_dir))
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (WEB_DIR / "setup.html").read_text(encoding="utf-8")

    @app.get("/api/info")
    def info() -> dict[str, Any]:
        return {"video": s.info.filename, "duration_s": s.audit.duration_s,
                "fps": s.audit.effective_fps, "warnings": s.audit.warnings,
                "pictures": [t.as_dict() for t in s.tiles], "configs": s.existing()}

    @app.get("/api/picture/{i}.jpg")
    def picture(i: int, t: float | None = None) -> Response:
        return Response(s.picture_jpeg(i, t), media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/config")
    def config(path: str, picture: int) -> dict[str, Any]:
        return s.shapes_of(path, picture)

    @app.post("/api/validate")
    def validate(d: Draft) -> dict[str, Any]:
        return s.evaluate(d)

    @app.post("/api/save")
    def save(d: Draft) -> dict[str, Any]:
        return s.save(d)

    return app
