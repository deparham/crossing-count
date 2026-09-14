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
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse

# "testserver" is the test client's name; no public name resolves to it
LOCAL_NAMES = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


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
