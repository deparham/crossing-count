"""Local head-marking page. Served from 127.0.0.1 only; frames never leave this computer.

Its URLs are relative, so it works on its own (label.py) and inside the count wizard
at /label/.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from av.error import FFmpegError
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from .heads import HeadLabels, LabelError, add_video
from .review_app import WEB_DIR
from .wizard import default_folders, list_videos


class AddIn(BaseModel):
    path: str
    per_camera: int = 30


class SaveIn(BaseModel):
    heads: list[list[float]] = Field(default_factory=list)
    done: bool = True
    skipped: bool = False


@dataclass
class _Adding:
    busy: bool = False
    video: str = ""
    added: int = 0
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


def create_label_app(root: Path | None = None, folders: list[Path] | None = None,
                     runs_root: Path | None = None) -> FastAPI:
    store = HeadLabels(root)
    from .localweb import local_only

    adding = _Adding()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    local_only(app)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (WEB_DIR / "label.html").read_text(encoding="utf-8")

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return {**store.stats(), "frames_list": store.summary(),
                "adding": {"busy": adding.busy, "video": adding.video, "added": adding.added,
                           "error": adding.error}}

    @app.get("/api/videos")
    def videos() -> dict[str, Any]:
        where = folders or default_folders()
        return {"folders": [str(f) for f in where], "videos": list_videos(where)}

    @app.post("/api/add")
    def add(a: AddIn) -> dict[str, Any]:
        path = Path(a.path).expanduser()
        if not path.is_file():
            raise HTTPException(400, f"There is no file at {path}")
        with adding.lock:
            if adding.busy:
                raise HTTPException(409, f"Still adding frames from {adding.video}.")
            adding.busy, adding.video, adding.added, adding.error = True, path.name, 0, None

        def work() -> None:
            try:
                adding.added = add_video(path, max(1, min(200, a.per_camera)), store.root,
                                         runs_root)
            except (OSError, ValueError, FFmpegError) as exc:
                adding.error = f"{path.name}: {exc}"
            finally:
                adding.busy = False

        threading.Thread(target=work, daemon=True).start()
        return {"started": True}

    @app.get("/api/frame/{fid}")
    def frame(fid: str) -> dict[str, Any]:
        try:
            return store.get(fid)
        except LabelError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/image/{fid}")
    def image(fid: str) -> FileResponse:
        try:
            return FileResponse(store.image(fid), media_type="image/jpeg")
        except LabelError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/frame/{fid}")
    def save(fid: str, s: SaveIn) -> dict[str, Any]:
        try:
            store.save(fid, s.heads, s.done, s.skipped)
        except LabelError as exc:
            raise HTTPException(400, str(exc)) from exc
        return store.stats()

    return app
