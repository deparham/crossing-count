"""Local count-wizard page. Served from 127.0.0.1 only; the video never leaves this Mac.

The drawing step is the setup page itself, mounted at /draw/<id>/ for the chosen
video and shown in a frame.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from av.error import FFmpegError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel
from starlette.routing import Mount

from . import overlay as ov
from . import paths
from .examples import check_folder
from .review_app import WEB_DIR, _range_response
from .webapp import Setup, create_app
from .wizard import Wizard, WizardError, default_folders, list_videos

PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class OpenIn(BaseModel):
    path: str


class DirectionIn(BaseModel):
    direction: str


class SensorIn(BaseModel):
    values: dict[str, int | None]


class ModelIn(BaseModel):
    model: str


class AnswerIn(BaseModel):
    id: str
    answer: str | None = None
    people: int = 1  # a "yes" for a group crossing together


class AddIn(BaseModel):
    camera: str
    t: float
    direction: str
    range: str | None = None


class IndexIn(BaseModel):
    index: int


class WatchIn(BaseModel):
    mode: str | None = None


class WatchedIn(BaseModel):
    range: str
    done: bool = True


class StoreIn(BaseModel):
    name: str | None = None
    code: str | None = None
    location: str | None = None
    report_date: str | None = None
    operator: str | None = None


class ModeIn(BaseModel):
    mode: str


class MarksIn(BaseModel):
    marks: bool


class NamedCamerasIn(BaseModel):
    cameras: list[dict[str, Any]]


class HandAddIn(BaseModel):
    camera: str
    t: float
    direction: str


class HandCameraIn(BaseModel):
    camera: str


class HandIdIn(BaseModel):
    id: int


class HandWatchedIn(BaseModel):
    camera: str
    start: float
    end: float


class HandPositionIn(BaseModel):
    camera: str
    t: float


class DoneIn(BaseModel):
    done: bool = True


class SettingsIn(BaseModel):
    examples_dir: str


@dataclass
class _Current:
    wizard: Wizard | None = None
    setup: Setup | None = None
    draw: str | None = None


def create_wizard_app(sites_dir: Path | None = None, runs_root: Path | None = None,
                      folders: list[Path] | None = None, logo: Path | None = None,
                      commands: Callable[[Wizard], list[list[str]]] | None = None) -> FastAPI:
    sites = sites_dir or paths.sites_dir()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    cur = _Current()
    from .heads_app import create_label_app  # the head-marking page, at /label/

    app.mount("/label", create_label_app(folders=folders, runs_root=runs_root))

    def wiz() -> Wizard:
        if cur.wizard is None:
            raise HTTPException(409, "Choose the footage first.")
        return cur.wizard

    def setup() -> Setup:
        if cur.setup is None:
            raise HTTPException(409, "Choose the footage first.")
        return cur.setup

    def public() -> dict[str, Any]:
        return {**wiz().public(), "draw_url": cur.draw,
                "logo": str(logo) if logo is not None and logo.is_file() else None,
                "pictures": [t.as_dict() for t in setup().tiles]}

    def run(change: Callable[[], object]) -> dict[str, Any]:
        try:
            change()
        except WizardError as exc:
            raise HTTPException(400, str(exc)) from exc
        return public()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (WEB_DIR / "wizard.html").read_text(encoding="utf-8")

    @app.get("/api/videos")
    def videos(folder: str | None = None) -> dict[str, Any]:
        where = [Path(folder).expanduser()] if folder else (folders or default_folders())
        return {"folders": [str(f) for f in where], "videos": list_videos(where)}

    @app.post("/api/open")
    def open_video(o: OpenIn) -> dict[str, Any]:
        path = Path(o.path).expanduser()
        if not path.is_file():
            raise HTTPException(400, f"There is no file at {path}")
        try:
            w = Wizard(path, sites, runs_root, commands)
            s = Setup(w.video, sites)
        except (WizardError, ValueError, OSError, FFmpegError) as exc:
            raise HTTPException(400, f"Could not open {path.name}: {exc}") from exc
        draw = f"/draw/{uuid.uuid4().hex[:10]}"
        app.router.routes[:] = [r for r in app.router.routes
                                if not (isinstance(r, Mount) and r.path.startswith("/draw/"))]
        app.mount(draw, create_app(w.video, sites, setup=s))
        cur.wizard, cur.setup, cur.draw = w, s, draw + "/"
        return public()

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return public()

    @app.get("/api/drawn")
    def drawn() -> dict[str, Any]:
        s = setup()
        cfgs = s.existing()
        marks = s.marks()
        return {"pictures": [
            {"picture": t.index, "marks": bool(marks[t.index]["marks"]),
             "configs": [{"sensor": c["sensor"], "file": c["file"]} for c in cfgs
                         if c["picture"] == t.index and not c["problem"]]}
            for t in s.tiles],
            # RetailNext's blue lines found on the people-free picture, or a saved drawing
            # whose recorded blue line matches this video
            "marks_guess": any(bool(m["marks"]) for m in marks) or any(
                c["picture"] is not None and c["match"] is not None
                and c["overlay_hue"] is not None
                and ov.MARK_HUES[0] <= c["overlay_hue"] <= ov.MARK_HUES[1] for c in cfgs)}

    @app.post("/api/mode")
    def mode(m: ModeIn) -> dict[str, Any]:
        return run(lambda: wiz().set_mode(m.mode))

    @app.post("/api/marks")
    def marks(m: MarksIn) -> dict[str, Any]:
        return run(lambda: wiz().set_marks(m.marks))

    @app.post("/api/named-cameras")
    def named_cameras(n: NamedCamerasIn) -> dict[str, Any]:
        s = setup()
        return run(lambda: wiz().set_named_cameras(s.existing(), [t.as_dict() for t in s.tiles],
                                                   n.cameras))

    def hand() -> dict[str, Any]:
        return {**public(), "hand": wiz().manual_summary()}

    def hand_run(change: Callable[[], object]) -> dict[str, Any]:
        run(change)
        return hand()

    @app.get("/api/hand")
    def hand_state() -> dict[str, Any]:
        return {**hand(), "geometry": wiz().geometry()}

    @app.post("/api/hand/add")
    def hand_add(a: HandAddIn) -> dict[str, Any]:
        return hand_run(lambda: wiz().manual_add(a.camera, a.t, a.direction))

    @app.post("/api/hand/delete")
    def hand_delete(i: HandIdIn) -> dict[str, Any]:
        return hand_run(lambda: wiz().manual_delete(i.id))

    @app.post("/api/hand/undo")
    def hand_undo(c: HandCameraIn) -> dict[str, Any]:
        removed: list[dict[str, Any] | None] = []
        out = hand_run(lambda: removed.append(wiz().manual_undo(c.camera)))
        return {**out, "removed": removed[0] if removed else None}

    @app.post("/api/hand/watched")
    def hand_watched(w: HandWatchedIn) -> dict[str, Any]:
        return hand_run(lambda: wiz().manual_watched(w.camera, w.start, w.end))

    @app.post("/api/hand/position")
    def hand_position(p: HandPositionIn) -> dict[str, Any]:
        return hand_run(lambda: wiz().manual_position(p.camera, p.t))

    @app.post("/api/hand/done")
    def hand_done(d: DoneIn) -> dict[str, Any]:
        return hand_run(lambda: wiz().manual_done(d.done))

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return {"examples_dir": paths.load_settings().get("examples_dir", "")}

    @app.post("/api/settings")
    def set_settings(s: SettingsIn) -> dict[str, Any]:
        if not s.examples_dir.strip():
            return {"examples_dir": paths.save_settings({"examples_dir": ""})["examples_dir"]}
        try:
            folder = check_folder(s.examples_dir.strip())
        except (OSError, ValueError) as exc:
            raise HTTPException(400, f"Can't use that folder: {exc}") from exc
        return {"examples_dir": paths.save_settings({"examples_dir": str(folder)})["examples_dir"]}

    @app.get("/api/examples")
    def examples() -> dict[str, Any]:
        return {"examples": wiz().state.get("examples")}

    @app.post("/api/cameras")
    def cameras() -> dict[str, Any]:
        s = setup()
        return run(lambda: wiz().set_cameras(s.existing(), [t.as_dict() for t in s.tiles]))

    @app.post("/api/direction")
    def direction(d: DirectionIn) -> dict[str, Any]:
        return run(lambda: wiz().set_direction(d.direction))

    @app.post("/api/sensor")
    def sensor(s: SensorIn) -> dict[str, Any]:
        return run(lambda: wiz().set_sensor(s.values))

    @app.post("/api/model")
    def model(m: ModelIn) -> dict[str, Any]:
        return run(lambda: wiz().set_model(m.model))

    @app.post("/api/run")
    def start() -> dict[str, Any]:
        return run(wiz().start)

    @app.post("/api/stop")
    def stop() -> dict[str, Any]:
        return run(wiz().stop)

    @app.get("/api/job")
    def job() -> dict[str, Any]:
        return wiz().job_status()

    @app.get("/api/preview.jpg")
    def preview(picture: int, t: float = 0.0) -> Response:
        try:
            data = wiz().preview_jpeg(picture, t)
        except WizardError as exc:
            raise HTTPException(404, str(exc)) from exc
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/check")
    def check() -> dict[str, Any]:
        w = wiz()
        return {"items": w.check_items(), "watch": w.watch_ranges(), "cameras": w.geometry(),
                "answers": w.state["answers"], "added": w.state["added"],
                "watched": w.state["watched"], "watch_mode": w.state["watch"],
                "counts": w.counts()}

    @app.post("/api/answer")
    def answer(a: AnswerIn) -> dict[str, Any]:
        return run(lambda: wiz().answer(a.id, a.answer, a.people))

    @app.post("/api/add")
    def add(a: AddIn) -> dict[str, Any]:
        return run(lambda: wiz().add(a.camera, a.t, a.direction, a.range))

    @app.post("/api/remove-added")
    def remove_added(i: IndexIn) -> dict[str, Any]:
        return run(lambda: wiz().remove_added(i.index))

    @app.post("/api/watch")
    def watch(m: WatchIn) -> dict[str, Any]:
        return run(lambda: wiz().set_watch(m.mode))

    @app.post("/api/watched")
    def watched(r: WatchedIn) -> dict[str, Any]:
        return run(lambda: wiz().mark_watched(r.range, r.done))

    @app.post("/api/store")
    def store(s: StoreIn) -> dict[str, Any]:
        return run(lambda: wiz().set_store(s.name, s.code, s.location, s.report_date,
                                           s.operator))

    @app.post("/api/report")
    def report() -> dict[str, Any]:
        w = wiz()
        try:
            path = w.make_report(logo)
        except WizardError as exc:
            raise HTTPException(400, str(exc)) from exc
        folder = paths.load_settings().get("examples_dir")
        if folder:  # learning examples go to the shared folder in the background
            try:
                w.save_examples(check_folder(folder))
            except (OSError, ValueError) as exc:
                w.state["examples"] = {"status": "failed", "error": f"{folder}: {exc}"}
        return {"path": str(path), "name": path.name, **public()}

    @app.get("/api/report.pptx")
    def report_file() -> FileResponse:
        made = wiz().state.get("report")
        if not made or not Path(made["path"]).is_file():
            raise HTTPException(404, "No report yet.")
        path = Path(made["path"])
        return FileResponse(path, media_type=PPTX, filename=path.name)

    @app.post("/api/reveal")
    def reveal() -> dict[str, bool]:
        made = wiz().state.get("report")
        if made and sys.platform == "darwin":
            subprocess.run(["/usr/bin/open", "-R", made["path"]], check=False)
        elif made and sys.platform == "win32":
            subprocess.run(["explorer", f"/select,{made['path']}"], check=False)
        return {"ok": bool(made)}

    @app.get("/video")
    def video_file(request: Request) -> Response:
        return _range_response(wiz().video, request.headers.get("range"))

    return app
