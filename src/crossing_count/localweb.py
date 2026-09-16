"""CrossingCount's local server answers only its own pages, on this computer.

The server listens on 127.0.0.1, so nothing outside the computer can reach it. But any
web page open in a browser on the same computer can still send it requests, and a site
that points its own name at 127.0.0.1 ("DNS rebinding") could even read the answers:
footage, pictures of people, reports. So every request must be addressed to this
computer by its own name (the Host header), and a request that changes anything must not
come from another site (the Origin header, which browsers set and pages cannot forge).
Programs that are not browsers (the tests, curl) send no Origin and are let through.

With sharing switched on (network.py), requests from other computers reach it too: those
need the access code, then its cookie, and are refused whatever stays with this computer.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Collection
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse

from . import network

if TYPE_CHECKING:
    from .network import Sharing

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


def allowed(method: str, host: str | None, origin: str | None,
            names: Collection[str] = LOCAL_NAMES) -> bool:
    """Is this request from CrossingCount's own pages (or a program that is not a browser)?"""
    if not host or _hostname(host) not in names:
        return False
    if method.upper() in SAFE_METHODS or origin is None:
        return True
    o = urlsplit(origin)  # "null" (a sandboxed or file page) has no scheme: refused
    return o.scheme in ("http", "https") and o.netloc.lower() == host.strip().lower()


ADMITTED = "crossingcount.admitted"  # set on a request once let in: pages mounted inside trust it
JOIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>CrossingCount</title>
<style>body{font:16px system-ui,sans-serif;background:#f5f8f9;color:#101d3b;margin:0;
display:grid;place-items:center;min-height:100vh}form{background:#fff;padding:28px;
border-radius:10px;box-shadow:0 2px 12px #0002;max-width:360px;width:calc(100% - 32px)}
input{font:22px ui-monospace,monospace;letter-spacing:3px;width:100%;box-sizing:border-box;
padding:8px;margin:12px 0;text-transform:uppercase}button{font:inherit;padding:8px 16px}
.err{color:#b03a2e}</style></head><body><form method="post" action="/join">
<h2>CrossingCount</h2><div>Enter the access code shown on the computer running
CrossingCount.</div><input name="code" autocomplete="off" autofocus placeholder="XXXX-XXXX">
<input type="hidden" name="next" value="{next}"><div class="err">{error}</div>
<button type="submit">Open</button></form></body></html>"""
WRONG = "That code is not right. It changes each time sharing is switched on."
LOCKED = "Too many wrong codes: try again in a few minutes."


def _join_page(error: str = "", next_path: str = "/", status: int = 401) -> HTMLResponse:
    safe_next = next_path if next_path.startswith("/") and not next_path.startswith("//") else "/"
    page = JOIN_PAGE.replace("{error}", error).replace(
        "{next}", safe_next.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;"))
    return HTMLResponse(page, status_code=status)


def _let_in(sharing: Sharing, code: str, next_path: str) -> Response:
    access = sharing.access
    token = access.join(code) if access is not None else None
    if token is None:
        locked = access is None or access.locked_out()
        return _join_page(LOCKED if locked else WRONG, next_path, 429 if locked else 401)
    safe_next = next_path if next_path.startswith("/") and not next_path.startswith("//") else "/"
    out = RedirectResponse(safe_next, status_code=303)
    out.set_cookie(network.COOKIE, token, httponly=True, samesite="strict", path="/")
    return out


async def _from_network(request: Request, call_next: Callable[[Request], Awaitable[Response]],
                        sharing: Sharing | None, host_only: Collection[tuple[str, str]]
                        ) -> Response:
    """A request from another computer: in with the code or its cookie, never for what stays
    with this computer, and never sent by another site's page."""
    if sharing is None or not sharing.on:
        return PlainTextResponse("CrossingCount answers only its own pages on this computer.",
                                 status_code=403)
    path, method = request.url.path, request.method.upper()
    token = request.cookies.get(network.COOKIE)
    if path == "/join":
        if method == "POST":
            form = parse_qs((await request.body()).decode("utf-8", "replace"))
            return _let_in(sharing, (form.get("code") or [""])[0], (form.get("next") or ["/"])[0])
        return _join_page(status=200)
    if not sharing.admitted(token):
        code = request.query_params.get("code")
        if method == "GET" and code:  # the link with the code in it: then a clean address
            rest = urlencode([(k, v) for k, v in request.query_params.multi_items() if k != "code"])
            return _let_in(sharing, code, path + (f"?{rest}" if rest else ""))
        if method == "GET" and not path.startswith("/api/") and "/api/" not in path:
            return _join_page(next_path=path)
        return PlainTextResponse("Enter the access code first: open this address in a browser.",
                                 status_code=401)
    origin = request.headers.get("origin")
    if method not in SAFE_METHODS and origin is not None:
        o = urlsplit(origin)
        if o.scheme not in ("http", "https") or o.netloc.lower() != (
                request.headers.get("host") or "").strip().lower():
            return PlainTextResponse("Refused: sent from another site's page.", status_code=403)
    if (method, path) in host_only:
        return PlainTextResponse("Only on the computer running CrossingCount: this stays with "
                                 "that computer.", status_code=403)
    request.scope[ADMITTED] = True
    assert token is not None  # admitted
    network.CLIENT.set(request.client.host if request.client else "?")
    network.SESSION.set(hashlib.sha256(token.encode()).hexdigest()[:16])
    return await call_next(request)


def local_only(app: FastAPI, sharing: Sharing | None = None,
               host_only: Collection[tuple[str, str]] = ()) -> None:
    """Only this computer's own pages; and, with sharing on, people on the network with the
    access code (never for host_only: (method, path) pairs that stay with this computer)."""
    @app.middleware("http")
    async def guard(request: Request,
                    call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.scope.get(ADMITTED):  # mounted inside an app that already let it in
            return await call_next(request)
        if not network.on_this_computer(request.client.host if request.client else None):
            return await _from_network(request, call_next, sharing, host_only)
        # while shared, this computer's own network names reach it too (on this computer the
        # name resolves to 127.0.0.1): names an attacker's site cannot use
        names = LOCAL_NAMES | sharing.names() if sharing is not None and sharing.on else LOCAL_NAMES
        if not allowed(request.method, request.headers.get("host"),
                       request.headers.get("origin"), names):
            return PlainTextResponse("CrossingCount answers only its own pages on this computer.",
                                     status_code=403)
        request.scope[ADMITTED] = True
        network.CLIENT.set(None)
        network.SESSION.set("local")
        return await call_next(request)
