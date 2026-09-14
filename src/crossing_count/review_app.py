"""Local review page (M3). Served from 127.0.0.1 only; the video never leaves this Mac.

The browser plays the original MP4 (H.264) through a byte-range endpoint and
draws each camera's picture, geometry and the proposal's path on a canvas.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from .config import ConfigError, bind, load_config
from .review import DECISIONS, TAGS, Camera, ReviewError, ReviewSession
from .video import audit_timebase, parse_filename_interval, probe

WEB_DIR = Path(__file__).parent / "web"
CHUNK = 4 << 20  # largest byte range served per request


class DecisionIn(BaseModel):
    id: str
    decision: str
    direction: str | None = None
    tags: list[str] = Field(default_factory=list)
    added: list[dict[str, Any]] = Field(default_factory=list)
    seconds: float | None = None


class OperatorIn(BaseModel):
    name: str


class DraftIn(BaseModel):
    id: str
    direction: str | None = None
    tags: list[str] = Field(default_factory=list)
    added: list[dict[str, Any]] = Field(default_factory=list)


def _pts(a: Any) -> list[list[float]]:
    return [[round(float(x), 1), round(float(y), 1)] for x, y in a]


def camera_geometry(cam: Camera) -> dict[str, Any] | None:
    """The camera's line and zones in frame pixels, for drawing over the video."""
    t = cam.candidates["picture"]
    try:
        cfg = load_config(cam.candidates["config"]["path"])
        g = bind(cfg, t["x1"] - t["x0"], t["y1"] - t["y0"], (t["x0"], t["y0"]))
    except (OSError, ConfigError):
        return None
    return {
        "line": _pts(g.line),
        "inside_sign": g.inside_sign,
        "mask_zone": _pts(g.mask_zone) if g.mask_zone is not None else None,
        "filter_zones": [_pts(z) for z in g.filter_zones],
        "exclusion_zones": [_pts(z) for z in g.exclusion_zones],
    }


def _range_response(path: Path, header: str | None) -> Response:
    size = path.stat().st_size
    if not header or not header.startswith("bytes="):
        return FileResponse(path, media_type="video/mp4", headers={"Accept-Ranges": "bytes"})
    first, _, last = header[6:].split(",")[0].strip().partition("-")
    try:
        if first == "":
            start, end = max(0, size - int(last)), size - 1
        else:
            start = int(first)
            end = int(last) if last else size - 1
    except ValueError:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    end = min(end, size - 1, start + CHUNK - 1)
    if start >= size or start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    with path.open("rb") as f:
        f.seek(start)
        data = f.read(end - start + 1)
    return Response(data, status_code=206, media_type="video/mp4", headers={
        "Content-Range": f"bytes {start}-{end}/{size}", "Accept-Ranges": "bytes",
        "Content-Length": str(len(data))})


def create_review_app(video: str | Path, run_dir: str | Path, operator: str = "",
                      seed: int = 0, discard_sample: float = 0.25) -> FastAPI:
    video_path, run = Path(video), Path(run_dir)
    session = ReviewSession(run, operator, seed, discard_sample)
    info = probe(video_path)
    if info.fingerprint != session.cams[0].candidates["video"]["fingerprint"]:
        raise ReviewError(f"{video_path.name} is not the video detect.py processed for {run}")
    audit = audit_timebase(video_path)
    interval = parse_filename_interval(video_path.name)
    cameras = {c.sensor: {"tile": c.candidates["picture"], "geometry": camera_geometry(c),
                          "rule": c.candidates["config"]["rule"]} for c in session.cams}
    records: dict[str, Any] = {}
    for c in session.cams:
        for group, key in ((c.candidates, "candidates"), (c.discarded, "discarded"),
                           (c.unexplained, "unexplained")):
            for rec in group[key]:
                records[rec["id"]] = rec

    from .localweb import local_only

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    local_only(app)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (WEB_DIR / "review.html").read_text(encoding="utf-8")

    @app.get("/api/session")
    def get_session() -> dict[str, Any]:
        return {
            "video": info.filename, "duration_s": audit.duration_s,
            "fps": audit.effective_fps,
            "clock_start": interval.start if interval else None,
            "tz": interval.tz if interval else "",
            "operator": session.operator, "cameras": cameras, "items": session.items,
            "decisions": session.decisions, "progress": session.progress(),
            "tags": list(TAGS), "decisions_allowed": DECISIONS, "warnings": session.warnings,
        }

    @app.get("/api/item/{item_id}")
    def item(item_id: str) -> dict[str, Any]:
        if item_id not in session.index:
            raise HTTPException(404, f"no item {item_id}")
        return {"item": session.index[item_id], "record": records.get(item_id),
                "decision": session.decisions.get(item_id),
                "draft": session.drafts.get(item_id)}

    @app.post("/api/draft")
    def draft(d: DraftIn) -> dict[str, bool]:
        try:
            session.save_draft(d.id, direction=d.direction, tags=d.tags, added=d.added)
        except ReviewError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"saved": True}

    @app.post("/api/decide")
    def decide(d: DecisionIn) -> dict[str, Any]:
        try:
            return session.decide(d.id, d.decision, direction=d.direction, tags=d.tags,
                                  added=d.added, seconds=d.seconds)
        except ReviewError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/undo")
    def undo() -> dict[str, Any]:
        return {"item": session.undo(), "progress": session.progress()}

    @app.post("/api/operator")
    def set_operator(o: OperatorIn) -> dict[str, str]:
        session.set_operator(o.name)
        return {"operator": session.operator}

    @app.get("/video")
    def video_file(request: Request) -> Response:
        return _range_response(video_path, request.headers.get("range"))

    @app.get("/report", response_class=HTMLResponse)
    def report() -> str:
        path = run / "export" / "report.html"
        if not path.is_file():
            return ("<!doctype html><meta charset=utf-8><title>No report yet</title>"
                    "<body style='font:15px system-ui;padding:40px'>No report yet: finish the "
                    "review, then run <code>uv run export.py VIDEO</code> and reload this page."
                    "</body>")
        return path.read_text(encoding="utf-8")

    return app
