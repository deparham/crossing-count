"""Local manual-count page. Served from 127.0.0.1 only; the video never leaves this Mac."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from .manual import ManualError, ManualSession
from .review_app import WEB_DIR, _range_response


class CountIn(BaseModel):
    camera: int
    t: float
    direction: str


class DeleteIn(BaseModel):
    id: int


class CameraIn(BaseModel):
    camera: int


class WatchedIn(BaseModel):
    camera: int
    start: float
    end: float


class PositionIn(BaseModel):
    camera: int
    t: float


class NameIn(BaseModel):
    camera: int
    name: str


class MetaIn(BaseModel):
    operator: str | None = None
    site: str | None = None
    notes: str | None = None


class SensorIn(BaseModel):
    interval: str
    camera: int | None = None
    direction: str
    value: int | None = None


def create_manual_app(session: ManualSession) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def run(change: Callable[[], object]) -> dict[str, Any]:
        try:
            change()
        except ManualError as exc:
            raise HTTPException(400, str(exc)) from exc
        return session.public()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (WEB_DIR / "count.html").read_text(encoding="utf-8")

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return session.public()

    @app.post("/api/count")
    def count(c: CountIn) -> dict[str, Any]:
        return run(lambda: session.add(c.camera, c.t, c.direction))

    @app.post("/api/delete")
    def delete(d: DeleteIn) -> dict[str, Any]:
        return run(lambda: session.delete(d.id))

    @app.post("/api/undo")
    def undo(c: CameraIn) -> dict[str, Any]:
        removed: list[dict[str, Any] | None] = []
        out = run(lambda: removed.append(session.undo(c.camera)))
        return {**out, "removed": removed[0] if removed else None}

    @app.post("/api/watched")
    def watched(w: WatchedIn) -> dict[str, Any]:
        return run(lambda: session.watched(w.camera, w.start, w.end))

    @app.post("/api/position")
    def position(p: PositionIn) -> dict[str, Any]:
        return run(lambda: session.set_position(p.camera, p.t))

    @app.post("/api/camera")
    def camera(n: NameIn) -> dict[str, Any]:
        return run(lambda: session.rename_camera(n.camera, n.name))

    @app.post("/api/meta")
    def meta(m: MetaIn) -> dict[str, Any]:
        return run(lambda: session.set_meta(m.operator, m.site, m.notes))

    @app.post("/api/sensor")
    def sensor(s: SensorIn) -> dict[str, Any]:
        return run(lambda: session.set_sensor(s.interval, s.camera, s.direction, s.value))

    @app.post("/api/export")
    def export() -> dict[str, Any]:
        return {"written": [str(p) for p in session.export()]}

    @app.get("/video")
    def video_file(request: Request) -> Response:
        return _range_response(session.video_path, request.headers.get("range"))

    @app.get("/report", response_class=HTMLResponse)
    def report() -> str:
        return session.report_html()

    return app
