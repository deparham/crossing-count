"""CrossingCount's local server answers only its own pages, on this computer.

The server listens on 127.0.0.1, so nothing outside the computer can reach it. But any
web page open in a browser on the same computer can still send it requests, and a site
that points its own name at 127.0.0.1 ("DNS rebinding") could even read the answers:
footage, pictures of people, reports. So every request must be addressed to this
computer by its own name (the Host header), and a request that changes anything must not
come from another site (the Origin header, which browsers set and pages cannot forge).
Programs that are not browsers (the tests, curl) send no Origin and are let through.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse

# "testserver" is the test client's name; no public name resolves to it
LOCAL_NAMES = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
WEB_DIR = Path(__file__).parent / "web"  # the pages this server answers with
CHUNK = 4 << 20  # largest byte range of video served per request


def video_range(path: Path, header: str | None) -> Response:
    """Part of a video, for a page's player: the browser asks for byte ranges, and the file
    never leaves this computer."""
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


def _hostname(host: str) -> str:
    h = host.strip().lower()
    if h.startswith("["):  # [::1]:8780
        return h[1:h.find("]")] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def allowed(method: str, host: str | None, origin: str | None) -> bool:
    """Is this request from CrossingCount's own pages (or a program that is not a browser)?"""
    if not host or _hostname(host) not in LOCAL_NAMES:
        return False
    if method.upper() in SAFE_METHODS or origin is None:
        return True
    o = urlsplit(origin)  # "null" (a sandboxed or file page) has no scheme: refused
    return o.scheme in ("http", "https") and o.netloc.lower() == host.strip().lower()


def local_only(app: FastAPI) -> None:
    @app.middleware("http")
    async def guard(request: Request,
                    call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if not allowed(request.method, request.headers.get("host"),
                       request.headers.get("origin")):
            return PlainTextResponse("CrossingCount answers only its own pages on this computer.",
                                     status_code=403)
        return await call_next(request)
